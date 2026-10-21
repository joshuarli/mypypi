from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import sys
import zipfile

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from mypypi.acquire import Release, ReleaseFile
from mypypi.build import acquire_or_build, sha256
from mypypi.models import ErrorKind, MypypiError, PackageVersion, Target, ValidatedArtifact
from mypypi.platforms import verify_repair_tool
from mypypi.store import publish
from mypypi.validate import inspect_wheel, validate_wheel


def _host_target() -> Target:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return Target(Version("3.15"), f"macos-{'arm64' if machine == 'aarch64' else machine}")
    if sys.platform.startswith("linux"):
        return Target(Version("3.15"), f"linux-{'aarch64' if machine == 'arm64' else machine}")
    pytest.skip(f"native fixture is unsupported on {sys.platform}-{machine}")


def _source_archive() -> bytes:
    files = {
        "native_fixture-1.0/pyproject.toml": (
            "[build-system]\nrequires = [\"setuptools>=80\"]\n"
            "build-backend = \"setuptools.build_meta\"\n"
        ),
        "native_fixture-1.0/setup.py": (
            "from setuptools import Extension, setup\n"
            "setup(name='native-fixture', version='1.0', packages=['native_fixture'], "
            "ext_modules=[Extension('native_fixture._native', ['native_fixture/native.c'])])\n"
        ),
        "native_fixture-1.0/native_fixture/__init__.py": "from ._native import value\n",
        "native_fixture-1.0/native_fixture/native.c": (
            "#include <Python.h>\n"
            "static PyObject *value(PyObject *self, PyObject *args) { return PyLong_FromLong(315); }\n"
            "static PyMethodDef methods[] = {{\"value\", value, METH_NOARGS, NULL}, {NULL, NULL, 0, NULL}};\n"
            "static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, \"_native\", NULL, -1, methods};\n"
            "PyMODINIT_FUNC PyInit__native(void) { return PyModule_Create(&module); }\n"
        ),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


@pytest.mark.skipif(sys.version_info[:2] != (3, 15), reason="the project supports CPython 3.15")
def test_native_sdist_build_repair_validation_publication_and_import(tmp_path) -> None:
    target = _host_target()
    try:
        verify_repair_tool(target)
    except MypypiError as cause:
        if cause.kind is ErrorKind.DEPENDENCY:
            pytest.skip(str(cause))
        raise
    if shutil.which("cc") is None and shutil.which("clang") is None:
        pytest.skip("a native C compiler is unavailable")

    package = PackageVersion.parse("native-fixture", "1.0")
    source_bytes = _source_archive()
    source = ReleaseFile(
        "native_fixture-1.0.zip",
        "https://files.pythonhosted.org/native_fixture-1.0.zip",
        hashlib.sha256(source_bytes).hexdigest(),
        "sdist",
    )
    release = Release(package, SpecifierSet(">=3.15,<3.16"), (), (source,))
    artifacts = tmp_path / "artifacts"
    wheel = acquire_or_build(
        release,
        target,
        artifacts,
        index_url="https://pypi.org/simple",
        opener=lambda _: io.BytesIO(source_bytes),
    )
    metadata = inspect_wheel(wheel, target)
    published = tmp_path / "published"
    current = publish(
        (
            ValidatedArtifact(
                metadata.identity,
                wheel,
                sha256(wheel),
                frozenset({target}),
            ),
        ),
        published,
    )
    stored_wheel = published / "wheels" / wheel.name
    validated = validate_wheel(
        stored_wheel,
        target=target,
        wheel_set=published / "wheels",
    )

    assert current.is_symlink()
    assert stored_wheel.is_file()
    assert validated.identity == metadata.identity
    assert os.readlink(current).startswith("generations/")
