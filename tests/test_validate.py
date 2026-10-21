from __future__ import annotations

import hashlib
import io
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
    _uv_environment,
    inspect_wheel,
    validate_wheel,
)

FIXTURES = Path(__file__).parent / "fixtures" / "wheels"
TARGET = Target(Version("3.15"), "macos-arm64")


def make_wheel(
    directory: Path,
    *,
    filename: str = "validation_fixture-1.0-py3-none-any.whl",
    module: str = "validation_fixture.py",
    module_source: str | bytes = "value = 1\n",
    requires_python: str = ">=3.15,<3.16",
    requires_dist: tuple[str, ...] = (),
    tag: str = "py3-none-any",
    record_override: bytes | None = None,
) -> Path:
    path = directory / filename
    dist_info = "validation_fixture-1.0.dist-info"
    metadata = [
        "Metadata-Version: 2.4",
        "Name: validation-fixture",
        "Version: 1.0",
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
            f"Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\n"
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
    elf = bytearray(20)
    elf[:6] = b"\x7fELF\x02\x01"
    elf[18:20] = (62).to_bytes(2, "little")
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
        index_url="https://curated.example/simple",
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
    environment = _uv_environment()
    assert "UV_INDEX" not in environment
    assert "UV_EXTRA_INDEX_URL" not in environment
    assert "UV_INDEX_PYPI_URL" not in environment


def test_runtime_install_is_limited_to_curated_index_wheels_and_binaries():
    command = _install_command(
        python=Path("/env/bin/python"),
        wheel=Path("/wheels/validation_fixture.whl"),
        wheel_set=Path("/wheels"),
        index_url="https://curated.example/simple",
        policy=ValidationPolicy(),
    )
    assert "--only-binary=:all:" in command
    assert "--default-index=https://curated.example/simple" in command
    assert "--index-strategy=first-index" in command
    assert "--find-links=/wheels" in command
    assert not any("pypi.org" in argument for argument in command)
    with pytest.raises(ValueError, match="public PyPI"):
        _install_command(
            python=Path("/env/bin/python"),
            wheel=Path("/wheels/validation_fixture.whl"),
            wheel_set=Path("/wheels"),
            index_url="https://pypi.org/simple",
            policy=ValidationPolicy(),
        )


def test_validate_wheel_installs_from_local_wheel_set_and_imports(
    tmp_path, monkeypatch
):
    wheel = FIXTURES / "validation_fixture-1.0-py3-none-any.whl"
    monkeypatch.setenv("TMPDIR", "/Users/josh/dev/mypypi/.work/tmp")
    validated = validate_wheel(
        wheel,
        target=TARGET,
        wheel_set=FIXTURES,
        index_url="http://127.0.0.1:9/simple",
    )
    assert validated.imports == ("validation_fixture",)


def test_validate_wheel_fails_when_import_raises(tmp_path, monkeypatch):
    wheel = make_wheel(
        tmp_path,
        module_source="raise RuntimeError('broken import')\n",
    )
    monkeypatch.setenv("TMPDIR", "/Users/josh/dev/mypypi/.work/tmp")
    monkeypatch.setenv("UV_HTTP_RETRIES", "0")
    with pytest.raises(MypypiError, match="import validation_fixture failed") as result:
        validate_wheel(
            wheel,
            target=TARGET,
            wheel_set=tmp_path,
            index_url="http://127.0.0.1:9/simple",
        )
    assert result.value.context.package == PackageVersion.parse(
        "validation-fixture", "1.0"
    )


def test_validate_wheel_skips_only_the_named_import(tmp_path, monkeypatch):
    wheel = make_wheel(
        tmp_path,
        module_source="raise RuntimeError('broken import')\n",
    )
    monkeypatch.setenv("TMPDIR", "/Users/josh/dev/mypypi/.work/tmp")
    validate_wheel(
        wheel,
        target=TARGET,
        wheel_set=tmp_path,
        index_url="http://127.0.0.1:9/simple",
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
    monkeypatch.setenv("TMPDIR", "/Users/josh/dev/mypypi/.work/tmp")
    monkeypatch.setenv("UV_HTTP_RETRIES", "0")
    with pytest.raises(MypypiError, match="dependency installation failed") as result:
        validate_wheel(
            wheel,
            target=TARGET,
            wheel_set=tmp_path,
            index_url="http://127.0.0.1:9/simple",
        )
    assert result.value.context.target == TARGET
