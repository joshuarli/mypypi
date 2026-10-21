from __future__ import annotations

import hashlib
import io
import json
import stat
import urllib.error
import zipfile

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from mypypi.acquire import (
    Release,
    ReleaseFile,
    compatible_wheels,
    download_artifact,
    release_from_metadata,
    validate_wheel_architectures,
)
from mypypi.models import MypypiError, PackageVersion, Target

PACKAGE = PackageVersion.parse("sample", "1.0")
TARGET = Target(Version("3.15"), "macos-arm64")


def test_compatible_wheels_prefers_cpython_then_abi3_then_pure() -> None:
    files = tuple(ReleaseFile(name, f"https://files.pythonhosted.org/{name}", "0" * 64, "bdist_wheel") for name in (
        "sample-1.0-py3-none-any.whl",
        "sample-1.0-cp314-abi3-macosx_11_0_arm64.whl",
        "sample-1.0-cp315-cp315-macosx_11_0_arm64.whl",
    ))
    release = Release(PACKAGE, SpecifierSet("<3.16"), files, None)
    assert [item.filename for item in compatible_wheels(release, TARGET)] == [
        "sample-1.0-cp315-cp315-macosx_11_0_arm64.whl",
        "sample-1.0-cp314-abi3-macosx_11_0_arm64.whl",
        "sample-1.0-py3-none-any.whl",
    ]


def test_manylinux_floor_reuses_older_compatible_wheels() -> None:
    target = Target(Version("3.15"), "linux-x86_64")
    wheel = ReleaseFile(
        "sample-1.0-cp315-cp315-manylinux_2_17_x86_64.whl",
        "https://files.pythonhosted.org/sample.whl",
        "0" * 64,
        "bdist_wheel",
    )
    release = Release(PACKAGE, SpecifierSet("<3.16"), (wheel,), None)
    assert compatible_wheels(release, target) == (wheel,)


def test_macos_targets_accept_universal2_wheels() -> None:
    wheel = ReleaseFile(
        "sample-1.0-cp315-cp315-macosx_11_0_universal2.whl",
        "https://files.pythonhosted.org/sample.whl",
        "0" * 64,
        "bdist_wheel",
    )
    release = Release(PACKAGE, SpecifierSet("<3.16"), (wheel,), None)
    for platform_name in ("macos-x86_64", "macos-arm64"):
        target = Target(Version("3.15"), platform_name)
        assert compatible_wheels(release, target) == (wheel,)


def test_wheel_architecture_check_ignores_executable_directory_entries(tmp_path) -> None:
    wheel = tmp_path / "sample-1.0-cp315-cp315-macosx_11_0_arm64.whl"
    binary = bytearray(32)
    binary[:4] = b"\xcf\xfa\xed\xfe"
    binary[4:8] = (0x0100000C).to_bytes(4, "little")
    directory = zipfile.ZipInfo("sample-1.0.dist-info/")
    directory.create_system = 3
    directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(directory, b"")
        archive.writestr("sample/_native.so", binary)

    validate_wheel_architectures(wheel, TARGET)


def test_release_metadata_rejects_yanked_and_mismatched_identity() -> None:
    metadata = {
        "info": {"name": "sample", "version": "1.0", "requires_python": ">=3.10"},
        "urls": [
            {
                "filename": "sample-1.0-py3-none-any.whl",
                "url": "https://files.pythonhosted.org/sample.whl",
                "packagetype": "bdist_wheel",
                "digests": {"sha256": "a" * 64},
                "yanked": "bad release",
            }
        ],
    }
    assert release_from_metadata(PACKAGE, metadata).wheels == ()
    assert release_from_metadata(PACKAGE, metadata, allow_yanked_reason="required fixture").wheels[0].yanked
    metadata["info"]["version"] = "2.0"
    with pytest.raises(MypypiError, match="metadata"):
        release_from_metadata(PACKAGE, metadata)


def test_release_metadata_rejects_non_https_urls() -> None:
    metadata = {
        "info": {"name": "sample", "version": "1.0"},
        "urls": [
            {
                "filename": "sample-1.0.tar.gz",
                "url": "http://example.test/source.tar.gz",
                "packagetype": "sdist",
                "digests": {"sha256": "b" * 64},
            }
        ],
    }
    with pytest.raises(MypypiError, match="HTTPS"):
        release_from_metadata(PACKAGE, metadata)


def test_fetch_release_retries_only_not_found() -> None:
    from mypypi.acquire import fetch_release

    metadata = {"info": {"name": "sample", "version": "1.0"}, "urls": []}
    responses = iter(
        (
            urllib.error.HTTPError("https://pypi.org", 404, "missing", {}, None),
            io.BytesIO(json.dumps(metadata).encode()),
        )
    )
    waits = []

    def opener(_):
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    release = fetch_release(PACKAGE, opener=opener, sleep=waits.append, monotonic=lambda: 0, retry_timeout=2)
    assert release.package == PACKAGE
    assert waits == [1.0]


def test_download_verifies_sha256_and_removes_corrupt_file(tmp_path) -> None:
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    content = b"fixture"
    file = ReleaseFile(
        "sample-1.0.tar.gz",
        "https://files.pythonhosted.org/source.tar.gz",
        hashlib.sha256(b"wrong").hexdigest(),
        "sdist",
    )
    with pytest.raises(MypypiError, match="SHA-256"):
        download_artifact(file, tmp_path, opener=lambda _: Response(content))
    assert not (tmp_path / file.filename).exists()


def test_download_rejects_artifact_path_traversal(tmp_path) -> None:
    file = ReleaseFile(
        "../escape.tar.gz",
        "https://files.pythonhosted.org/source.tar.gz",
        hashlib.sha256(b"fixture").hexdigest(),
        "sdist",
    )
    with pytest.raises(MypypiError, match="unsafe artifact filename"):
        download_artifact(file, tmp_path)
    assert not (tmp_path.parent / "escape.tar.gz").exists()


def test_download_interruption_removes_partial_archive(tmp_path) -> None:
    class InterruptedResponse(io.BytesIO):
        def __init__(self):
            super().__init__(b"fixture")
            self.calls = 0

        def read(self, size=-1):
            self.calls += 1
            if self.calls == 1:
                return super().read(3)
            raise KeyboardInterrupt

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    file = ReleaseFile(
        "sample-1.0.tar.gz",
        "https://files.pythonhosted.org/source.tar.gz",
        hashlib.sha256(b"fixture").hexdigest(),
        "sdist",
    )
    with pytest.raises(KeyboardInterrupt):
        download_artifact(file, tmp_path, opener=lambda _: InterruptedResponse())
    assert tuple(tmp_path.iterdir()) == ()


def test_wheel_archive_traversal_is_rejected(tmp_path) -> None:
    for member in ("../escape.py", "C:/escape.py", "pkg\\escape.py"):
        path = tmp_path / "sample-1.0-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(member, "bad")
        with pytest.raises(MypypiError, match="unsafe wheel archive member"):
            validate_wheel_architectures(path, TARGET)


def test_wheel_archive_rejects_duplicate_and_special_members(tmp_path) -> None:
    duplicate = tmp_path / "duplicate-1.0-py3-none-any.whl"
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("sample.py", "first")
            archive.writestr("sample.py", "second")
    with pytest.raises(MypypiError, match="duplicate paths"):
        validate_wheel_architectures(duplicate, TARGET)

    special = tmp_path / "special-1.0-py3-none-any.whl"
    member = zipfile.ZipInfo("sample.py")
    member.external_attr = (0o010600 << 16)
    with zipfile.ZipFile(special, "w") as archive:
        archive.writestr(member, "bad")
    with pytest.raises(MypypiError, match="non-regular member"):
        validate_wheel_architectures(special, TARGET)


def test_wheel_filename_architecture_must_match_target(tmp_path) -> None:
    path = tmp_path / "sample-1.0-cp315-cp315-macosx_11_0_x86_64.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("sample.py", "value = 1")
    with pytest.raises(MypypiError, match="do not include target"):
        validate_wheel_architectures(path, TARGET)


def test_native_binary_architecture_must_match_target(tmp_path) -> None:
    path = tmp_path / "sample-1.0-cp315-cp315-macosx_11_0_arm64.whl"
    elf = bytearray(20)
    elf[:6] = b"\x7fELF\x02\x01"
    elf[18:20] = (62).to_bytes(2, "little")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("sample/native.so", elf)
    with pytest.raises(MypypiError, match=r"expected \['arm64'\]"):
        validate_wheel_architectures(path, TARGET)
