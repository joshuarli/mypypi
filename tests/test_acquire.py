from __future__ import annotations

import hashlib
import io
import json
import stat
import urllib.error
import urllib.request
import zipfile

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from mypypi.acquire import (
    Release,
    ReleaseFile,
    _HttpsRedirectHandler,
    compatible_wheels,
    download_artifact,
    release_from_metadata,
    select_or_download,
    validate_wheel_architectures,
    validate_wheel_metadata,
)
from mypypi.models import ErrorKind, MypypiError, PackageVersion, Target

PACKAGE = PackageVersion.parse("sample", "1.0")
TARGET = Target(Version("3.15"), "macos-arm64")


def _macho(architecture: str) -> bytes:
    content = bytearray(32)
    content[:4] = b"\xcf\xfa\xed\xfe"
    cpu = {"x86_64": 0x01000007, "arm64": 0x0100000C}[architecture]
    content[4:8] = cpu.to_bytes(4, "little")
    return bytes(content)


def _elf(architecture: str) -> bytes:
    content = bytearray(64)
    content[:7] = b"\x7fELF\x02\x01\x01"
    machine = {"x86_64": 62, "aarch64": 183}[architecture]
    content[18:20] = machine.to_bytes(2, "little")
    content[20:24] = (1).to_bytes(4, "little")
    content[52:54] = (64).to_bytes(2, "little")
    return bytes(content)


def _wheel(path, filename, members, *, dist_info_directory=None):
    from packaging.utils import parse_wheel_filename

    name, version, _, tags = parse_wheel_filename(filename)
    dist_info = dist_info_directory or f"{str(name).replace('-', '_')}-{version}.dist-info"
    content = dict(members)
    content[f"{dist_info}/METADATA"] = (
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nRequires-Python: >=3.15\n\n"
    ).encode()
    content[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: false\n"
        + "".join(f"Tag: {tag}\n" for tag in sorted(tags))
        + "\n"
    ).encode()
    content[f"{dist_info}/RECORD"] = b""
    with zipfile.ZipFile(path, "w") as archive:
        for member, value in content.items():
            archive.writestr(member, value)
    return path.read_bytes()


def test_compatible_wheels_prefers_cpython_then_abi3_then_pure() -> None:
    files = tuple(ReleaseFile(name, f"https://files.pythonhosted.org/{name}", "0" * 64, "bdist_wheel") for name in (
        "sample-1.0-py3-none-any.whl",
        "sample-1.0-cp314-abi3-macosx_11_0_arm64.whl",
        "sample-1.0-cp315-cp315-macosx_11_0_arm64.whl",
    ))
    release = Release(PACKAGE, SpecifierSet("<3.16"), files, ())
    assert [item.filename for item in compatible_wheels(release, TARGET)] == [
        "sample-1.0-cp315-cp315-macosx_11_0_arm64.whl",
        "sample-1.0-cp314-abi3-macosx_11_0_arm64.whl",
        "sample-1.0-py3-none-any.whl",
    ]


def test_acquisition_accepts_case_preserved_normalized_dist_info_name(tmp_path):
    package = PackageVersion.parse("pysocks", "1.7.1")
    filename = "PySocks-1.7.1-py3-none-any.whl"
    path = tmp_path / filename
    _wheel(
        path,
        filename,
        {},
        dist_info_directory="PySocks-1.7.1.dist-info",
    )

    assert validate_wheel_metadata(path, package, TARGET).package == package


def test_manylinux_floor_reuses_older_compatible_wheels() -> None:
    target = Target(Version("3.15"), "linux-x86_64")
    wheel = ReleaseFile(
        "sample-1.0-cp315-cp315-manylinux_2_17_x86_64.whl",
        "https://files.pythonhosted.org/sample.whl",
        "0" * 64,
        "bdist_wheel",
    )
    release = Release(PACKAGE, SpecifierSet("<3.16"), (wheel,), ())
    assert compatible_wheels(release, target) == (wheel,)


def test_upstream_file_requires_python_filters_a_wheel():
    target = Target(Version("3.15"), "linux-x86_64")
    wheel = ReleaseFile(
        "sample-1.0-py3-none-any.whl",
        "https://files.pythonhosted.org/sample.whl",
        "0" * 64,
        "bdist_wheel",
        requires_python=SpecifierSet(">=3.15.1"),
    )
    release = Release(PACKAGE, SpecifierSet("<3.16"), (wheel,), ())

    assert compatible_wheels(release, target) == ()


def test_generic_linux_tag_is_not_a_portable_candidate() -> None:
    target = Target(Version("3.15"), "linux-x86_64")
    wheel = ReleaseFile(
        "sample-1.0-cp315-cp315-linux_x86_64.whl",
        "https://files.pythonhosted.org/sample.whl",
        "0" * 64,
        "bdist_wheel",
    )
    release = Release(PACKAGE, SpecifierSet("<3.16"), (wheel,), ())
    assert compatible_wheels(release, target) == ()


def test_macos_targets_accept_universal2_wheels() -> None:
    wheel = ReleaseFile(
        "sample-1.0-cp315-cp315-macosx_11_0_universal2.whl",
        "https://files.pythonhosted.org/sample.whl",
        "0" * 64,
        "bdist_wheel",
    )
    release = Release(PACKAGE, SpecifierSet("<3.16"), (wheel,), ())
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


def test_release_metadata_rejects_credentials_in_https_urls():
    with pytest.raises(MypypiError, match="credential-free HTTPS"):
        release_from_metadata(
            PACKAGE,
            {
                "info": {"name": "sample", "version": "1.0"},
                "urls": [
                    {
                        "filename": "sample-1.0.zip",
                        "url": "https://user:password@files.pythonhosted.org/sample.zip",
                        "packagetype": "sdist",
                        "digests": {"sha256": "a" * 64},
                    }
                ],
            },
        )


def test_https_redirect_handler_rejects_downgrades_and_credentials():
    request = urllib.request.Request("https://files.pythonhosted.org/sample.whl")
    handler = _HttpsRedirectHandler()

    for destination in (
        "http://files.pythonhosted.org/sample.whl",
        "https://user:password@files.pythonhosted.org/sample.whl",
    ):
        with pytest.raises(urllib.error.URLError, match="credential-free HTTPS"):
            handler.redirect_request(request, None, 302, "Found", {}, destination)


def test_release_metadata_retains_file_level_requires_python():
    filename = "sample-1.0-py3-none-any.whl"
    release = release_from_metadata(
        PACKAGE,
        {
            "info": {"name": "sample", "version": "1.0", "requires_python": ">=3.15"},
            "urls": [
                {
                    "filename": filename,
                    "url": f"https://files.pythonhosted.org/{filename}",
                    "packagetype": "bdist_wheel",
                    "digests": {"sha256": "a" * 64},
                    "requires_python": ">=3.15.1",
                }
            ],
        },
    )

    assert release.wheels[0].requires_python == SpecifierSet(">=3.15.1")


def test_release_metadata_retains_every_sdist() -> None:
    metadata = {
        "info": {"name": "sample", "version": "1.0"},
        "urls": [
            {
                "filename": filename,
                "url": f"https://files.pythonhosted.org/{filename}",
                "packagetype": "sdist",
                "digests": {"sha256": "c" * 64},
            }
            for filename in ("sample-1.0.tar.gz", "sample-1.0.zip")
        ],
    }
    release = release_from_metadata(PACKAGE, metadata)
    assert tuple(item.filename for item in release.sdists) == (
        "sample-1.0.tar.gz",
        "sample-1.0.zip",
    )


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


def test_download_reuses_identical_bytes_and_rejects_conflicting_bytes(tmp_path) -> None:
    content = b"fixture"
    file = ReleaseFile(
        "sample-1.0.tar.gz",
        "https://files.pythonhosted.org/source.tar.gz",
        hashlib.sha256(content).hexdigest(),
        "sdist",
    )
    first = download_artifact(file, tmp_path, opener=lambda _: io.BytesIO(content))
    assert download_artifact(file, tmp_path, opener=lambda _: pytest.fail("unexpected download")) == first
    first.write_bytes(b"different")
    with pytest.raises(MypypiError) as result:
        download_artifact(file, tmp_path, opener=lambda _: pytest.fail("unexpected download"))
    assert result.value.kind is ErrorKind.CONFLICT


def test_select_or_download_tries_ranked_wheel_alternatives_in_staging(tmp_path) -> None:
    first_name = "sample-1.0-cp315-cp315-macosx_15_0_arm64.whl"
    second_name = "sample-1.0-cp314-abi3-macosx_11_0_arm64.whl"
    first_bytes = _wheel(
        tmp_path / first_name,
        first_name,
        {"sample/_native.so": _macho("x86_64")},
    )
    second_bytes = _wheel(
        tmp_path / second_name,
        second_name,
        {"sample/_native.so": _macho("arm64")},
    )
    artifacts = tuple(
        ReleaseFile(
            name,
            f"https://files.pythonhosted.org/{name}",
            hashlib.sha256(content).hexdigest(),
            "bdist_wheel",
        )
        for name, content in ((first_name, first_bytes), (second_name, second_bytes))
    )
    release = Release(PACKAGE, SpecifierSet("<3.16"), artifacts, ())
    contents = {
        f"https://files.pythonhosted.org/{first_name}": first_bytes,
        f"https://files.pythonhosted.org/{second_name}": second_bytes,
    }

    result = select_or_download(
        release,
        TARGET,
        tmp_path / "artifacts",
        opener=lambda url: io.BytesIO(contents[url]),
    )

    assert result is not None and result.name == second_name
    assert tuple(path.name for path in (tmp_path / "artifacts").iterdir()) == (second_name,)


def test_select_or_download_reuses_a_valid_identical_wheel(tmp_path) -> None:
    filename = "sample-1.0-py3-none-any.whl"
    content = _wheel(tmp_path / filename, filename, {"sample.py": b"value = 1"})
    artifact = ReleaseFile(
        filename,
        f"https://files.pythonhosted.org/{filename}",
        hashlib.sha256(content).hexdigest(),
        "bdist_wheel",
    )
    destination = tmp_path / "artifacts"
    destination.mkdir()
    existing = destination / filename
    existing.write_bytes(content)
    release = Release(PACKAGE, SpecifierSet("<3.16"), (artifact,), ())

    result = select_or_download(
        release,
        TARGET,
        destination,
        opener=lambda _: pytest.fail("unexpected download"),
    )

    assert result == existing


def test_malformed_native_header_is_an_integrity_failure(tmp_path) -> None:
    path = tmp_path / "sample-1.0-cp315-cp315-macosx_15_0_arm64.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("sample/_native.so", b"\x7fELF")

    with pytest.raises(MypypiError) as result:
        validate_wheel_architectures(path, TARGET)

    assert result.value.kind is ErrorKind.INTEGRITY


def test_acquisition_metadata_accepts_normalized_local_version_dist_info(tmp_path) -> None:
    package = PackageVersion.parse("sample", "1.0+local")
    filename = "sample-1.0+local-py3-none-any.whl"
    wheel = tmp_path / filename
    _wheel(wheel, filename, {"sample.py": b"value = 1"})

    assert validate_wheel_metadata(wheel, package, TARGET).package == package


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
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("sample/native.so", _elf("x86_64"))
    with pytest.raises(MypypiError, match=r"expected \['arm64'\]"):
        validate_wheel_architectures(path, TARGET)
