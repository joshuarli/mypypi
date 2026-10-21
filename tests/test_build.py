from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from packaging.version import Version

from mypypi.acquire import ReleaseFile
from mypypi.build import (
    build_from_sdist,
    likely_binary,
    produced_binary,
    require_native_target,
    validate_wheel_identity,
)
from mypypi.models import MypypiError, PackageVersion, Target

PACKAGE = PackageVersion.parse("sample", "1.0")
TARGET = Target(Version("3.15"), "macos-arm64")


def archive(path, files):
    with zipfile.ZipFile(path, "w") as output:
        for name, content in files.items():
            output.writestr(name, content)
    return path


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


def test_likely_binary_rejects_archive_traversal(tmp_path) -> None:
    source = archive(tmp_path / "sample.zip", {"../escape.c": "bad"})
    with pytest.raises(ValueError, match="unsafe sdist member"):
        likely_binary(source)


def test_produced_binary_detects_shared_library(tmp_path) -> None:
    wheel = archive(tmp_path / "sample-1.0-py3-none-any.whl", {"sample/_native.so": b"native"})
    assert produced_binary(wheel)


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


def test_build_from_sdist_uses_uv_backend_and_repairs_native_wheel(tmp_path) -> None:
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

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        output = kwargs["cwd"] / "wheels" if command[0] == "uv" else kwargs["cwd"] / "repaired"
        output.mkdir(exist_ok=True)
        wheel = output / "sample-1.0-cp315-cp315-macosx_11_0_arm64.whl"
        if command[0] == "uv":
            archive(wheel, {"sample/_native.so": b"\xcf\xfa\xed\xfe" + b"\x0c\x00\x00\x01" + b"\x00" * 20})
        else:
            shutil.copy(kwargs["cwd"] / "wheels" / wheel.name, wheel)

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


def test_build_from_sdist_runs_custom_prebuild_without_a_shell(tmp_path) -> None:
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
            archive(output / "sample-1.0-py3-none-any.whl", {"sample.py": "value = 1"})

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
    assert prebuild[2] == str(options["cwd"])
    assert options["cwd"].name == "prefix"
    assert "shell" not in options
    assert options["env"]["TMPDIR"].startswith(str(options["cwd"].parent))


@pytest.mark.parametrize("cancel", [False, True])
def test_build_from_sdist_cleans_staging_after_failure_and_cancellation(
    tmp_path, cancel
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
