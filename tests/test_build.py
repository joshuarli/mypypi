from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from mypypi.acquire import CompatibilityCategory, CompatibilityRejection, Release, ReleaseFile
from mypypi.build import (
    acquire_or_build,
    build_from_sdist,
    likely_binary,
    produced_binary,
    require_native_target,
    validate_wheel_identity,
)
from mypypi.models import MypypiError, PackageVersion, Target
from mypypi.process_policy import run_process

PACKAGE = PackageVersion.parse("sample", "1.0")
TARGET = Target(Version("3.15"), "macos-arm64")


@pytest.fixture
def synthetic_native_host(monkeypatch):
    monkeypatch.setattr("mypypi.build.require_native_target", lambda target: None)


def archive(path, files):
    with zipfile.ZipFile(path, "w") as output:
        for name, content in files.items():
            output.writestr(name, content)
    return path


def wheel_archive(path, filename, files):
    from packaging.utils import parse_wheel_filename

    name, version, _, tags = parse_wheel_filename(filename)
    dist_info = f"{str(name).replace('-', '_')}-{version}.dist-info"
    content = dict(files)
    content[f"{dist_info}/METADATA"] = (
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nRequires-Python: >=3.15\n\n"
    ).encode()
    content[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: false\n"
        + "".join(f"Tag: {tag}\n" for tag in sorted(tags))
        + "\n"
    ).encode()
    content[f"{dist_info}/RECORD"] = b""
    with zipfile.ZipFile(path, "w") as output:
        for name, value in content.items():
            output.writestr(name, value)
    return path


def macho(architecture):
    content = bytearray(32)
    content[:4] = b"\xcf\xfa\xed\xfe"
    cpu = {"x86_64": 0x01000007, "arm64": 0x0100000C}[architecture]
    content[4:8] = cpu.to_bytes(4, "little")
    return bytes(content)


def test_likely_binary_detects_compiled_sources_and_honors_ignore(tmp_path) -> None:
    source = archive(
        tmp_path / "sample.zip",
        {
            "sample-1.0/setup.py": "from setuptools import setup",
            "sample-1.0/src/extension.pyx": "cdef int x",
        },
    )
    assert likely_binary(source) == "source archive contains compiled-source files: .pyx"
    assert likely_binary(source, ("sample-1.0/src/extension.pyx",)) is None


def test_likely_binary_detects_bundled_native_payload_without_suffix(tmp_path) -> None:
    source = archive(
        tmp_path / "sample.zip",
        {"sample-1.0/setup.py": b"from setuptools import setup", "sample-1.0/data/helper": macho("arm64")},
    )
    assert likely_binary(source) == "source archive contains native payloads: sample-1.0/data/helper"


def test_likely_binary_rejects_archive_traversal(tmp_path) -> None:
    source = archive(tmp_path / "sample.zip", {"../escape.c": "bad"})
    with pytest.raises(ValueError, match="unsafe sdist member"):
        likely_binary(source)


def test_produced_binary_detects_shared_library(tmp_path) -> None:
    wheel = archive(tmp_path / "sample-1.0-py3-none-any.whl", {"sample/_native.so": b"native"})
    assert produced_binary(wheel)


def test_produced_binary_detects_native_payload_without_a_suffix(tmp_path) -> None:
    wheel = archive(
        tmp_path / "sample-1.0-py3-none-any.whl",
        {"sample/resources/helper": macho("arm64")},
    )
    assert produced_binary(wheel)


def test_produced_binary_detects_a_compiled_executable_and_ignores_other_binary_data(
    tmp_path,
) -> None:
    compiler = shutil.which("cc") or shutil.which("clang")
    if compiler is None:
        pytest.skip("a native C compiler is unavailable")
    source = tmp_path / "worker.c"
    executable = tmp_path / "worker"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    run_process(
        (compiler, str(source), "-o", str(executable)),
        timeout=60,
        check=True,
        capture_output=True,
    )
    native_wheel = tmp_path / "native-1.0-py3-none-any.whl"
    with zipfile.ZipFile(native_wheel, "w") as archive_file:
        archive_file.write(executable, "native/resources/worker")
    assert produced_binary(native_wheel)

    arbitrary_wheel = tmp_path / "data-1.0-py3-none-any.whl"
    member = zipfile.ZipInfo("data/resources/blob")
    member.external_attr = 0o100755 << 16
    with zipfile.ZipFile(arbitrary_wheel, "w") as archive_file:
        archive_file.writestr(member, b"\x00\x01arbitrary binary data")
    assert not produced_binary(arbitrary_wheel)


def test_produced_binary_rejects_malformed_native_headers_structurally(tmp_path) -> None:
    wheel = archive(tmp_path / "sample-1.0-py3-none-any.whl", {"sample/resources/helper": b"\x7fELF"})
    with pytest.raises(MypypiError) as result:
        produced_binary(wheel)
    assert result.value.kind.value == "integrity"


def test_wheel_filename_must_match_requested_package() -> None:
    with pytest.raises(MypypiError, match="does not match"):
        validate_wheel_identity(Path("other-1.0-py3-none-any.whl"), PACKAGE)


def test_native_build_rejects_cross_architecture_targets(monkeypatch) -> None:
    import mypypi.build as build

    monkeypatch.setattr(build.os.sys, "platform", "darwin")
    monkeypatch.setattr(build.platform, "machine", lambda: "arm64")
    require_native_target(Target(Version("3.15"), "macos-arm64"))
    with pytest.raises(MypypiError, match="host is macos-arm64"):
        require_native_target(Target(Version("3.15"), "macos-x86_64"))


def test_build_from_sdist_uses_uv_backend_and_repairs_native_wheel(
    tmp_path,
    monkeypatch,
    synthetic_native_host,
) -> None:
    source_bytes = io.BytesIO()
    with zipfile.ZipFile(source_bytes, "w") as source_zip:
        source_zip.writestr("sample-1.0/setup.py", "from setuptools import setup")
        source_zip.writestr("sample-1.0/native.pyx", "cdef int value")
    data = source_bytes.getvalue()
    source = ReleaseFile(
        "sample-1.0.zip",
        "https://files.pythonhosted.org/sample.zip",
        hashlib.sha256(data).hexdigest(),
        "sdist",
    )
    calls = []
    monkeypatch.setattr("mypypi.build.verify_repair_tool", lambda *args, **kwargs: "delocate 0.13.0")

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "uv":
            output = kwargs["cwd"] / "wheels"
            output.mkdir(exist_ok=True)
            wheel_archive(
                output / "sample-1.0-cp315-cp315-macosx_11_0_arm64.whl",
                "sample-1.0-cp315-cp315-macosx_11_0_arm64.whl",
                {"sample/_native.so": macho("arm64")},
            )
        else:
            output = kwargs["cwd"] / "repaired"
            output.mkdir(exist_ok=True)
            wheel_archive(
                output / "sample-1.0-cp315-cp315-macosx_15_0_arm64.whl",
                "sample-1.0-cp315-cp315-macosx_15_0_arm64.whl",
                {"sample/_native.so": macho("arm64")},
            )

    def opener(_):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()
        return Response(data)

    result = build_from_sdist(
        PACKAGE,
        TARGET,
        source,
        tmp_path / "artifacts",
        index_url="https://pypi.org/simple",
        runner=runner,
        env={"PATH": "/usr/bin"},
        opener=opener,
    )
    assert result.exists()
    assert calls[0][0][0] == "uv"
    assert calls[0][1]["env"]["MACOSX_DEPLOYMENT_TARGET"] == "15.0"
    assert calls[1][0][1] == "-mdelocate.cmd.delocate_wheel"


def test_build_rejects_a_pure_wheel_for_another_python_version(
    tmp_path,
    synthetic_native_host,
) -> None:
    data = archive(
        tmp_path / "source.zip",
        {"sample-1.0/setup.py": "from setuptools import setup"},
    ).read_bytes()
    source = ReleaseFile(
        "sample-1.0.zip",
        "https://files.pythonhosted.org/sample.zip",
        hashlib.sha256(data).hexdigest(),
        "sdist",
    )

    def runner(command, **kwargs):
        if command[0] == "uv":
            output = kwargs["cwd"] / "wheels"
            output.mkdir(exist_ok=True)
            wheel_archive(
                output / "sample-1.0-cp314-cp314-macosx_15_0_arm64.whl",
                "sample-1.0-cp314-cp314-macosx_15_0_arm64.whl",
                {"sample.py": b"value = 1"},
            )

    def opener(_):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        return Response(data)

    with pytest.raises(CompatibilityRejection) as result:
        build_from_sdist(
            PACKAGE,
            TARGET,
            source,
            tmp_path / "artifacts",
            index_url="https://pypi.org/simple",
            runner=runner,
            env={"PATH": "/usr/bin"},
            opener=opener,
        )

    assert result.value.category is CompatibilityCategory.WHEEL_TAG
    assert not tuple((tmp_path / "artifacts").glob(".mypypi-build-*"))


def test_build_from_sdist_runs_custom_prebuild_without_a_shell(tmp_path, synthetic_native_host) -> None:
    data = archive(
        tmp_path / "source.zip",
        {"sample-1.0/setup.py": "from setuptools import setup"},
    ).read_bytes()
    source = ReleaseFile(
        "sample-1.0.zip",
        "https://files.pythonhosted.org/sample.zip",
        hashlib.sha256(data).hexdigest(),
        "sdist",
    )
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "uv":
            output = kwargs["cwd"] / "wheels"
            output.mkdir(exist_ok=True)
            wheel_archive(
                output / "sample-1.0-py3-none-any.whl",
                "sample-1.0-py3-none-any.whl",
                {"sample.py": b"value = 1"},
            )

    def opener(_):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        return Response(data)

    build_from_sdist(
        PACKAGE,
        TARGET,
        source,
        tmp_path / "artifacts",
        index_url="https://pypi.org/simple",
        custom_prebuild=(("prepare --prefix {prefix}", "generate a native header"),),
        runner=runner,
        env={"PATH": "/usr/bin"},
        opener=opener,
    )

    prebuild, options = calls[0]
    assert prebuild[0] == "prepare"
    assert prebuild[1] == "--prefix"
    assert Path(prebuild[2]).name == "prefix"
    assert options["cwd"] == Path.cwd()
    assert "shell" not in options
    assert options["env"]["TMPDIR"].startswith(str(tmp_path))


def test_acquire_or_build_continues_across_sdist_compatibility_rejections(tmp_path, monkeypatch) -> None:
    import mypypi.build as build

    sources = (
        ReleaseFile("sample-1.0.tar.gz", "https://files.pythonhosted.org/a.tar.gz", "a" * 64, "sdist"),
        ReleaseFile("sample-1.0.zip", "https://files.pythonhosted.org/a.zip", "b" * 64, "sdist"),
    )
    release = Release(PACKAGE, SpecifierSet("<3.16"), (), sources)
    attempts = []
    expected_path = tmp_path / "sample-1.0-py3-none-any.whl"

    def build_sdist(package, target, source, destination, **kwargs):
        attempts.append(source.filename)
        if len(attempts) == 1:
            raise CompatibilityRejection(
                CompatibilityCategory.ARCHITECTURE,
                "wrong native architecture",
                build.FailureContext(build.FailureStage.REPAIR, package, target),
            )
        return expected_path

    monkeypatch.setattr(build, "build_from_sdist", build_sdist)
    result = acquire_or_build(release, TARGET, tmp_path, index_url="https://pypi.org/simple")

    assert result == expected_path
    assert attempts == ["sample-1.0.tar.gz", "sample-1.0.zip"]


def test_acquire_or_build_does_not_retry_unclassified_build_errors(tmp_path, monkeypatch) -> None:
    import mypypi.build as build

    sources = (
        ReleaseFile("sample-1.0.tar.gz", "https://files.pythonhosted.org/a.tar.gz", "a" * 64, "sdist"),
        ReleaseFile("sample-1.0.zip", "https://files.pythonhosted.org/a.zip", "b" * 64, "sdist"),
    )
    release = Release(PACKAGE, SpecifierSet("<3.16"), (), sources)
    attempts = []

    def build_sdist(package, target, source, destination, **kwargs):
        attempts.append(source.filename)
        raise MypypiError(
            build.ErrorKind.BUILD,
            "backend failed",
            build.FailureContext(build.FailureStage.BUILD, package, target),
        )

    monkeypatch.setattr(build, "build_from_sdist", build_sdist)
    with pytest.raises(MypypiError) as result:
        acquire_or_build(release, TARGET, tmp_path, index_url="https://pypi.org/simple")

    assert result.value.kind.value == "build"
    assert attempts == ["sample-1.0.tar.gz"]


@pytest.mark.parametrize("cancel", [False, True])
def test_build_from_sdist_cleans_staging_after_failure_and_cancellation(
    tmp_path, cancel, synthetic_native_host
) -> None:
    data = archive(
        tmp_path / "source.zip",
        {"sample-1.0/setup.py": "from setuptools import setup"},
    ).read_bytes()
    source = ReleaseFile(
        "sample-1.0.zip",
        "https://files.pythonhosted.org/sample.zip",
        hashlib.sha256(data).hexdigest(),
        "sdist",
    )

    def runner(command, **kwargs):
        if cancel:
            raise KeyboardInterrupt
        raise subprocess.CalledProcessError(2, command, stderr="backend failed")

    def opener(_):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        return Response(data)

    def build():
        return build_from_sdist(
            PACKAGE,
            TARGET,
            source,
            tmp_path / "artifacts",
            index_url="https://pypi.org/simple",
            runner=runner,
            env={"PATH": "/usr/bin"},
            opener=opener,
        )
    if cancel:
        with pytest.raises(KeyboardInterrupt):
            build()
    else:
        with pytest.raises(MypypiError, match="backend failed") as result:
            build()
        assert result.value.context.stage.value == "build"
    assert not tuple((tmp_path / "artifacts").glob(".mypypi-build-*"))
