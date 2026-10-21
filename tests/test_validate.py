from __future__ import annotations

import hashlib
import io
import sys
import zipfile
from base64 import urlsafe_b64encode
from pathlib import Path

import pytest
from packaging.version import Version

from mypypi.models import (
    ErrorKind,
    FailureStage,
    MypypiError,
    PackageVersion,
    Target,
)
from mypypi.validate import (
    ExceptionKind,
    ValidationException,
    ValidationPolicy,
    _install_command,
    _local_platform,
    _uv_environment,
    inspect_wheel,
    validate_wheel,
)

FIXTURES = Path(__file__).parent / "fixtures" / "wheels"
TARGET = Target(Version("3.15"), "macos-arm64")
HOST_PLATFORM = _local_platform()
assert HOST_PLATFORM is not None
HOST_TARGET = Target(Version("3.15"), HOST_PLATFORM)


def make_wheel(
    directory: Path,
    *,
    filename: str = "validation_fixture-1.0-py3-none-any.whl",
    module: str = "validation_fixture.py",
    module_source: str | bytes = "value = 1\n",
    requires_python: str = ">=3.15,<3.16",
    requires_dist: tuple[str, ...] = (),
    tag: str = "py3-none-any",
    version: str = "1.0",
    package_name: str = "validation-fixture",
    dist_info_directory: str | None = None,
    record_override: bytes | None = None,
    generator_headers: tuple[str, ...] = ("tests",),
) -> Path:
    path = directory / filename
    dist_info = dist_info_directory or (
        f"{package_name.replace('-', '_')}-{version.replace('-', '_')}.dist-info"
    )
    metadata = [
        "Metadata-Version: 2.4",
        f"Name: {package_name}",
        f"Version: {version}",
        f"Requires-Python: {requires_python}",
        *(f"Requires-Dist: {requirement}" for requirement in requires_dist),
        "",
        "",
    ]
    members = {
        module: (
            module_source.encode()
            if isinstance(module_source, str)
            else module_source
        ),
        f"{dist_info}/METADATA": "\n".join(metadata).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            + "".join(f"Generator: {generator}\n" for generator in generator_headers)
            + "Root-Is-Purelib: true\n"
            f"Tag: {tag}\n\n"
        ).encode(),
    }
    record = io.StringIO()
    for name, data in members.items():
        digest = urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        record.write(f"{name},sha256={digest},{len(data)}\n")
    record.write(f"{dist_info}/RECORD,,\n")
    members[f"{dist_info}/RECORD"] = (
        record_override if record_override is not None else record.getvalue().encode()
    )
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_inspect_wheel_checks_identity_structure_tags_and_requires_python():
    metadata = inspect_wheel(
        FIXTURES / "validation_fixture-1.0-py3-none-any.whl", TARGET
    )
    assert metadata.identity.package == PackageVersion.parse(
        "validation-fixture", "1.0"
    )
    assert metadata.imports == ("validation_fixture",)
    assert not metadata.native_architectures


def test_inspect_wheel_accepts_local_version_dist_info(tmp_path):
    wheel = make_wheel(
        tmp_path,
        filename="validation_fixture-1.0+local-py3-none-any.whl",
        version="1.0+local",
    )

    metadata = inspect_wheel(wheel, TARGET)

    assert str(metadata.identity.package.version) == "1.0+local"


def test_inspect_wheel_accepts_case_preserved_normalized_dist_info_name(tmp_path):
    wheel = make_wheel(
        tmp_path,
        filename="PySocks-1.7.1-py3-none-any.whl",
        version="1.7.1",
        package_name="PySocks",
        dist_info_directory="PySocks-1.7.1.dist-info",
    )

    metadata = inspect_wheel(wheel, TARGET)

    assert metadata.identity.package == PackageVersion.parse("pysocks", "1.7.1")


def test_inspect_wheel_accepts_generator_added_by_native_repair(tmp_path):
    wheel = make_wheel(
        tmp_path,
        generator_headers=("setuptools (84.2.1)", "delocate 0.13.0"),
    )

    metadata = inspect_wheel(wheel, TARGET)

    assert metadata.identity.package == PackageVersion.parse(
        "validation-fixture", "1.0"
    )


@pytest.mark.parametrize(
    ("filename", "requires_python", "expected"),
    [
        ("wrong_name-1.0-py3-none-any.whl", ">=3.15", "filename"),
        ("validation_fixture-1.0-cp314-cp314-macosx_11_0_arm64.whl", ">=3.15", "tags"),
        ("validation_fixture-1.0-py3-none-any.whl", ">=3.16", "Requires-Python"),
    ],
)
def test_inspect_wheel_rejects_wrong_identity_tags_and_python(
    filename, requires_python, expected, tmp_path
):
    wheel = make_wheel(tmp_path, filename=filename, requires_python=requires_python)
    with pytest.raises(MypypiError, match=expected) as result:
        inspect_wheel(wheel, TARGET)
    assert result.value.kind is ErrorKind.VALIDATION
    assert result.value.context.stage is FailureStage.VALIDATION
    expected_package = (
        PackageVersion.parse("wrong-name", "1.0")
        if filename.startswith("wrong_name")
        else PackageVersion.parse("validation-fixture", "1.0")
    )
    assert result.value.context.package == expected_package
    assert result.value.context.target == TARGET


def test_inspect_wheel_uses_the_running_cpython_patch_version(tmp_path):
    required = Version(
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro + 1}"
    )
    wheel = make_wheel(
        tmp_path,
        requires_python=f">={required},<3.16",
    )

    actual = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    with pytest.raises(MypypiError, match=f"Requires-Python excludes CPython {actual}"):
        inspect_wheel(wheel, HOST_TARGET)


def test_inspect_wheel_rejects_incomplete_or_unsafe_wheel(tmp_path):
    wheel = tmp_path / "validation_fixture-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("../escape.py", "pass\n")
    with pytest.raises(MypypiError, match="unsafe archive path"):
        inspect_wheel(wheel, TARGET)


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (
            b"validation_fixture.py,sha256=bad,10\n"
            b"validation_fixture-1.0.dist-info/METADATA,sha256=bad,1\n"
            b"validation_fixture-1.0.dist-info/WHEEL,sha256=bad,1\n"
            b"validation_fixture-1.0.dist-info/RECORD,,\n",
            "RECORD hash does not match",
        ),
        (
            b"validation_fixture-1.0.dist-info/RECORD,,\n",
            "RECORD does not match its files",
        ),
    ],
)
def test_inspect_wheel_rejects_invalid_record_hashes_and_missing_files(
    tmp_path, record, message
):
    wheel = make_wheel(tmp_path, record_override=record)

    with pytest.raises(MypypiError, match=message):
        inspect_wheel(wheel, TARGET)


def test_inspect_wheel_rejects_native_binary_for_wrong_architecture(tmp_path):
    elf = bytearray(64)
    elf[:7] = b"\x7fELF\x02\x01\x01"
    elf[18:20] = (62).to_bytes(2, "little")
    elf[20:24] = (1).to_bytes(4, "little")
    elf[52:54] = (64).to_bytes(2, "little")
    wheel = make_wheel(
        tmp_path,
        module="validation_fixture.abi3.so",
        module_source=bytes(elf),
    )
    with pytest.raises(MypypiError, match="target architecture arm64"):
        inspect_wheel(wheel, TARGET)


def test_inspect_wheel_accepts_linux_manylinux_tag_compatible_with_floor(tmp_path):
    wheel = make_wheel(
        tmp_path,
        filename=(
            "validation_fixture-1.0-cp315-cp315-manylinux_2_27_x86_64.whl"
        ),
        tag="cp315-cp315-manylinux_2_27_x86_64",
    )
    metadata = inspect_wheel(wheel, Target(Version("3.15"), "linux-x86_64"))
    assert metadata.identity.filename.endswith("manylinux_2_27_x86_64.whl")


def test_inspect_wheel_rejects_direct_runtime_dependency_urls(tmp_path):
    wheel = make_wheel(
        tmp_path,
        requires_dist=("remote-package @ https://example.invalid/remote.whl",),
    )
    with pytest.raises(MypypiError, match="direct URLs"):
        inspect_wheel(wheel, TARGET)


def test_validation_exception_requires_reason_and_names_one_exception():
    with pytest.raises(TypeError, match="ExceptionKind"):
        ValidationException("skip-import", "validation_fixture", "required reason")
    with pytest.raises(ValueError, match="requires an explanation"):
        ValidationException(ExceptionKind.SKIP_IMPORT, "validation_fixture", "")
    with pytest.raises(TypeError, match="immutable tuple"):
        ValidationPolicy([
            ValidationException(
                ExceptionKind.SKIP_IMPORT,
                "validation_fixture",
                "required reason",
            )
        ])
    with pytest.raises(ValueError, match="direct URL"):
        ValidationException(
            ExceptionKind.MISSING_DEPENDENCY,
            "remote-package @ https://example.invalid/remote.whl",
            "upstream omitted a runtime requirement",
        )
    policy = ValidationPolicy(
        (
            ValidationException(
                ExceptionKind.EXTRAS,
                "speedups",
                "exercise optional accelerator",
            ),
            ValidationException(
                ExceptionKind.MISSING_DEPENDENCY,
                "unlisted-dependency==2.0",
                "upstream omitted a runtime requirement",
            ),
            ValidationException(
                ExceptionKind.SKIP_IMPORT,
                "validation_fixture.native",
                "native module cannot load in this target environment",
            ),
        )
    )
    command = _install_command(
        python=Path("/env/bin/python"),
        wheel=Path("/wheels/validation_fixture.whl"),
        wheel_set=Path("/wheels"),
        policy=policy,
    )
    assert policy.extras == "speedups"
    assert policy.skipped_imports == {"validation_fixture.native"}
    assert command[-2:] == (
        "/wheels/validation_fixture.whl[speedups]",
        "unlisted-dependency==2.0",
    )


def test_uv_environment_cannot_add_an_unconfigured_package_index(monkeypatch):
    monkeypatch.setenv("UV_INDEX", "https://public.example/simple")
    monkeypatch.setenv("UV_EXTRA_INDEX_URL", "https://another.example/simple")
    monkeypatch.setenv("UV_INDEX_PYPI_URL", "https://named.example/simple")
    monkeypatch.setenv("UV_NO_BUILD_ISOLATION", "1")
    monkeypatch.setenv("UV_NO_VERIFY_HASHES", "1")
    monkeypatch.setenv("PIP_TRUSTED_HOST", "packages.example")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example")
    monkeypatch.setenv("SSL_CERT_FILE", "/certificates/custom.pem")
    environment = _uv_environment()
    assert "UV_INDEX" not in environment
    assert "UV_EXTRA_INDEX_URL" not in environment
    assert "UV_INDEX_PYPI_URL" not in environment
    assert "UV_NO_BUILD_ISOLATION" not in environment
    assert "UV_NO_VERIFY_HASHES" not in environment
    assert "PIP_TRUSTED_HOST" not in environment
    assert environment["HTTPS_PROXY"] == "https://proxy.example"
    assert environment["SSL_CERT_FILE"] == "/certificates/custom.pem"


def test_runtime_install_is_limited_to_curated_index_wheels_and_binaries():
    command = _install_command(
        python=Path("/env/bin/python"),
        wheel=Path("/wheels/validation_fixture.whl"),
        wheel_set=Path("/wheels"),
        policy=ValidationPolicy(),
    )
    assert "--only-binary=:all:" in command
    assert "--no-index" in command
    assert "--find-links=/wheels" in command
    assert not any("pypi.org" in argument for argument in command)


def test_validate_wheel_installs_from_local_wheel_set_and_imports(
    tmp_path, monkeypatch
):
    wheel = FIXTURES / "validation_fixture-1.0-py3-none-any.whl"
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    validated = validate_wheel(
        wheel,
        target=HOST_TARGET,
        wheel_set=FIXTURES,
    )
    assert validated.imports == ("validation_fixture",)


def test_validate_wheel_fails_when_import_raises(tmp_path, monkeypatch):
    wheel = make_wheel(
        tmp_path,
        module_source="raise RuntimeError('broken import')\n",
    )
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("UV_HTTP_RETRIES", "0")
    with pytest.raises(MypypiError, match="import validation_fixture failed") as result:
        validate_wheel(
            wheel,
            target=HOST_TARGET,
            wheel_set=tmp_path,
        )
    assert result.value.context.package == PackageVersion.parse(
        "validation-fixture", "1.0"
    )


def test_import_probe_ignores_cwd_and_pythonpath(tmp_path, monkeypatch):
    wheel = make_wheel(
        tmp_path,
        module_source="import ambient_only\nvalue = ambient_only.value\n",
    )
    (tmp_path / "ambient_only.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))

    with pytest.raises(MypypiError, match="import validation_fixture failed"):
        validate_wheel(
            wheel,
            target=HOST_TARGET,
            wheel_set=wheel.parent,
        )


def test_validate_wheel_skips_only_the_named_import(tmp_path, monkeypatch):
    wheel = make_wheel(
        tmp_path,
        module_source="raise RuntimeError('broken import')\n",
    )
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    validate_wheel(
        wheel,
        target=HOST_TARGET,
        wheel_set=tmp_path,
        policy=ValidationPolicy(
            (
                ValidationException(
                    ExceptionKind.SKIP_IMPORT,
                    "validation_fixture",
                    "module requires an unavailable native library",
                ),
            )
        ),
    )


def test_validate_wheel_fails_when_runtime_dependency_is_missing(tmp_path, monkeypatch):
    wheel = make_wheel(
        tmp_path,
        requires_dist=("missing-runtime-dependency==8.1",),
    )
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("UV_HTTP_RETRIES", "0")
    with pytest.raises(MypypiError, match="dependency installation failed") as result:
        validate_wheel(
            wheel,
            target=HOST_TARGET,
            wheel_set=tmp_path,
        )
    assert result.value.context.target == HOST_TARGET
