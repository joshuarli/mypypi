from __future__ import annotations

import csv
import email.parser
import hashlib
import importlib.machinery
import io
import os
import platform
import re
import subprocess
import sys
import tempfile
import zipfile
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import Tag, compatible_tags, cpython_tags, parse_tag
from packaging.version import InvalidVersion, Version

from .models import (
    ErrorKind,
    FailureContext,
    FailureStage,
    MypypiError,
    PackageVersion,
    Target,
    WheelIdentity,
)
from .platforms import native_architectures_from_bytes, target_platform_tags
from .process_policy import redacted_diagnostic, run_process, uv_environment
from .wheel_metadata import _matches_dist_info


class ExceptionKind(StrEnum):
    EXTRAS = "extras"
    MISSING_DEPENDENCY = "missing-dependency"
    SKIP_IMPORT = "skip-import"


@dataclass(frozen=True, slots=True)
class ValidationException:
    kind: ExceptionKind
    value: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExceptionKind):
            raise TypeError("validation exception kind must be an ExceptionKind")
        if not self.value or self.value.strip() != self.value:
            raise ValueError("validation exception value must be non-empty")
        if not self.reason or self.reason.strip() != self.reason:
            raise ValueError("validation exception requires an explanation")
        if self.kind is ExceptionKind.EXTRAS and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*(,[A-Za-z0-9][A-Za-z0-9._-]*)*",
            self.value,
        ):
            raise ValueError("validation extras must be comma-separated extra names")
        if self.kind is ExceptionKind.EXTRAS and len(set(self.value.split(","))) != len(
            self.value.split(",")
        ):
            raise ValueError("validation extras must be unique")
        if self.kind is ExceptionKind.SKIP_IMPORT and not all(
            part.isidentifier() for part in self.value.split(".")
        ):
            raise ValueError("skipped import must be a dotted Python name")
        if self.kind is ExceptionKind.MISSING_DEPENDENCY:
            try:
                requirement = Requirement(self.value)
            except InvalidRequirement as error:
                raise ValueError(
                    "missing dependency must be a valid requirement"
                ) from error
            if requirement.url is not None:
                raise ValueError("missing dependency cannot use a direct URL")


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    exceptions: tuple[ValidationException, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.exceptions, tuple):
            raise TypeError("validation exceptions must be an immutable tuple")
        keys = [(exception.kind, exception.value) for exception in self.exceptions]
        if len(keys) != len(set(keys)):
            raise ValueError("validation exceptions must be unique")
        extras_count = sum(
            exception.kind is ExceptionKind.EXTRAS for exception in self.exceptions
        )
        if extras_count > 1:
            raise ValueError("only one validation extras exception is allowed")

    @property
    def extras(self) -> str | None:
        return next(
            (
                exception.value
                for exception in self.exceptions
                if exception.kind is ExceptionKind.EXTRAS
            ),
            None,
        )

    @property
    def missing_dependencies(self) -> tuple[str, ...]:
        return tuple(
            exception.value
            for exception in self.exceptions
            if exception.kind is ExceptionKind.MISSING_DEPENDENCY
        )

    @property
    def skipped_imports(self) -> frozenset[str]:
        return frozenset(
            exception.value
            for exception in self.exceptions
            if exception.kind is ExceptionKind.SKIP_IMPORT
        )


@dataclass(frozen=True, slots=True)
class WheelMetadata:
    identity: WheelIdentity
    requires_python: SpecifierSet
    tags: frozenset[Tag]
    imports: tuple[str, ...]
    native_architectures: frozenset[str]


def _failure(
    wheel: WheelIdentity | None,
    target: Target,
    detail: str,
    *,
    cause: BaseException | None = None,
) -> MypypiError:
    context = FailureContext(
        FailureStage.VALIDATION,
        wheel.package if wheel is not None else None,
        target,
    )
    error = MypypiError(ErrorKind.VALIDATION, detail, context)
    if cause is not None:
        error.__cause__ = cause
    return error


def _platform_architecture(platform_name: str) -> str:
    match = re.fullmatch(r"(?:linux|macos)-(x86_64|aarch64|arm64)", platform_name)
    if match is None:
        raise ValueError(f"unsupported validation platform: {platform_name}")
    return match.group(1)


def _supported_tags(target: Target, linux_repair_policy: str) -> frozenset[Tag]:
    version = tuple(target.python_version.release[:2])
    platforms = target_platform_tags(target, linux_repair_policy)
    tags = cpython_tags(version, platforms=platforms)
    compatible = compatible_tags(version, platforms=platforms)
    return frozenset((*tags, *compatible))


def _local_platform() -> str | None:
    operating_system = platform.system()
    architecture = platform.machine().lower()
    if operating_system == "Darwin":
        return f"macos-{ {'aarch64': 'arm64'}.get(architecture, architecture) }"
    if operating_system == "Linux":
        return f"linux-{ {'arm64': 'aarch64'}.get(architecture, architecture) }"
    return None


def _metadata_from_wheel(
    path: Path, target: Target, linux_repair_policy: str
) -> WheelMetadata:
    try:
        identity = WheelIdentity.parse(path.name)
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                raise ValueError("wheel contains duplicate archive paths")
            for member in members:
                name = member.filename
                parts = name.rstrip("/").split("/")
                mode = member.external_attr >> 16
                if (
                    name.startswith("/")
                    or "\\" in name
                    or "\x00" in name
                    or not parts
                    or any(part in {"", ".", ".."} for part in parts)
                    or ":" in parts[0]
                ):
                    raise ValueError("wheel contains an unsafe archive path")
                if mode & 0o170000 == 0o120000:
                    raise ValueError("wheel contains a symbolic link")
            dist_infos = {
                name.split("/", 1)[0]
                for name in names
                if name.split("/", 1)[0].endswith(".dist-info")
            }
            if len(dist_infos) != 1:
                raise ValueError("wheel must contain exactly one .dist-info directory")
            dist_info = next(iter(dist_infos))
            if not _matches_dist_info(identity, dist_info):
                raise ValueError(
                    "wheel .dist-info directory does not match its filename"
                )
            metadata_path = f"{dist_info}/METADATA"
            wheel_path = f"{dist_info}/WHEEL"
            record_path = f"{dist_info}/RECORD"
            if not {metadata_path, wheel_path, record_path} <= set(names):
                raise ValueError("wheel requires METADATA, WHEEL, and RECORD")
            metadata = email.parser.BytesParser().parsebytes(
                archive.read(metadata_path)
            )
            wheel_headers = email.parser.BytesParser().parsebytes(
                archive.read(wheel_path)
            )
            metadata_versions = metadata.get_all("Metadata-Version", [])
            metadata_names = metadata.get_all("Name", [])
            metadata_package_versions = metadata.get_all("Version", [])
            requires_python_values = metadata.get_all("Requires-Python", [])
            wheel_versions = wheel_headers.get_all("Wheel-Version", [])
            purelib_values = wheel_headers.get_all("Root-Is-Purelib", [])
            generators = wheel_headers.get_all("Generator", [])
            if (
                len(metadata_versions) != 1
                or len(metadata_names) != 1
                or len(metadata_package_versions) != 1
                or len(requires_python_values) > 1
                or len(wheel_versions) != 1
                or len(purelib_values) != 1
                or not generators
            ):
                raise ValueError("wheel metadata contains missing or repeated singleton fields")
            wheel_version = wheel_versions[0].split(".")
            if (
                len(wheel_version) != 2
                or not all(part.isdecimal() for part in wheel_version)
                or int(wheel_version[0]) != 1
            ):
                raise ValueError("wheel uses an unsupported WHEEL format version")
            if purelib_values[0] not in {"true", "false"}:
                raise ValueError("wheel Root-Is-Purelib must be true or false")
            try:
                package = PackageVersion.parse(
                    metadata_names[0], metadata_package_versions[0]
                )
                requires_python = SpecifierSet(
                    requires_python_values[0] if requires_python_values else ""
                )
            except (TypeError, InvalidVersion, InvalidSpecifier) as error:
                raise ValueError("wheel has invalid package metadata") from error
            if package != identity.package:
                raise ValueError("wheel filename and METADATA identity differ")
            for dependency in metadata.get_all("Requires-Dist", []):
                try:
                    requirement = Requirement(dependency)
                except InvalidRequirement as error:
                    raise ValueError(
                        "wheel has an invalid Requires-Dist entry"
                    ) from error
                if requirement.url is not None:
                    raise ValueError("wheel dependencies cannot use direct URLs")
            wheel_tags = frozenset().union(
                *(parse_tag(value) for value in wheel_headers.get_all("Tag", []))
            )
            if not wheel_tags or wheel_tags != identity.tags:
                raise ValueError("wheel WHEEL tags do not match its filename")
            supported_tags = _supported_tags(target, linux_repair_policy)
            if not identity.tags.intersection(supported_tags):
                raise ValueError("wheel filename tags are incompatible with target")
            execution_version = target.python_version
            if (
                sys.implementation.name == "cpython"
                and sys.version_info[:2] == tuple(target.python_version.release[:2])
                and _local_platform() == target.platform
            ):
                execution_version = Version(
                    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
                )
            if not requires_python.contains(str(execution_version), prereleases=True):
                raise ValueError(
                    f"wheel Requires-Python excludes CPython {execution_version}"
                )
            rows = csv.reader(io.StringIO(archive.read(record_path).decode("utf-8")))
            recorded = {}
            for row in rows:
                if len(row) != 3 or not row[0]:
                    raise ValueError("wheel RECORD contains an invalid row")
                if row[0] in recorded:
                    raise ValueError(f"wheel RECORD duplicates {row[0]}")
                recorded[row[0]] = (row[1], row[2])
            file_names = {name for name in names if not name.endswith("/")}
            if set(recorded) != file_names:
                raise ValueError("wheel RECORD does not match its files")
            for name in sorted(file_names):
                digest_value, size_value = recorded[name]
                if name == record_path:
                    if digest_value or size_value:
                        raise ValueError("wheel RECORD row must leave RECORD hash and size empty")
                    continue
                if not size_value.isdecimal():
                    raise ValueError(f"wheel RECORD has an invalid size for {name}")
                try:
                    algorithm, separator, expected_digest = digest_value.partition("=")
                    if not separator or algorithm not in {"sha256", "sha384", "sha512"}:
                        raise ValueError(f"wheel RECORD has an invalid hash for {name}")
                    digest = hashlib.new(algorithm)
                    with archive.open(name) as member:
                        while chunk := member.read(64 * 1024):
                            digest.update(chunk)
                except (ValueError, OSError, zipfile.BadZipFile) as error:
                    raise ValueError(f"wheel RECORD has an invalid hash for {name}") from error
                encoded_digest = urlsafe_b64encode(digest.digest()).decode().rstrip("=")
                if encoded_digest != expected_digest:
                    raise ValueError(f"wheel RECORD hash does not match {name}")
                if int(size_value) != archive.getinfo(name).file_size:
                    raise ValueError(f"wheel RECORD size does not match {name}")
            imports: set[str] = set()
            native_architectures: set[str] = set()
            expected_arch = _platform_architecture(target.platform)
            for name in names:
                if name.endswith("/") or name.endswith(".dist-info/RECORD"):
                    continue
                with archive.open(name) as member:
                    data = member.read(4096)
                arches = native_architectures_from_bytes(data)
                if arches:
                    native_architectures.update(arches)
                    if expected_arch not in arches:
                        raise ValueError(
                            f"native artifact {name} does not contain target "
                            f"architecture {expected_arch}"
                        )
                parts = name.split("/")
                if parts[0].endswith((".dist-info", ".data")):
                    continue
                if len(parts) > 1 and any(
                    parts[-1].endswith(suffix)
                    for suffix in (".py", *importlib.machinery.EXTENSION_SUFFIXES)
                ):
                    if parts[0].isidentifier():
                        imports.add(parts[0])
                elif len(parts) == 1:
                    filename = parts[0]
                    if filename.endswith(".py"):
                        module = filename[:-3]
                    else:
                        suffix = next(
                            (
                                suffix
                                for suffix in sorted(
                                    importlib.machinery.EXTENSION_SUFFIXES,
                                    key=len,
                                    reverse=True,
                                )
                                if filename.endswith(suffix)
                            ),
                            None,
                        )
                        module = filename[: -len(suffix)] if suffix else ""
                    if module.isidentifier():
                        imports.add(module)
            return WheelMetadata(
                identity,
                requires_python,
                wheel_tags,
                tuple(sorted(imports)),
                frozenset(native_architectures),
            )
    except MypypiError:
        raise
    except (OSError, zipfile.BadZipFile, UnicodeError, ValueError, csv.Error) as cause:
        raise _failure(None, target, f"invalid wheel {path.name}: {cause}", cause=cause)


def inspect_wheel(
    path: Path,
    target: Target,
    linux_repair_policy: str = "manylinux_2_28",
) -> WheelMetadata:
    try:
        return _metadata_from_wheel(path, target, linux_repair_policy)
    except MypypiError as error:
        if error.context.package is None:
            try:
                error.context = FailureContext(
                    error.context.stage, WheelIdentity.parse(path.name).package, target
                )
            except ValueError:
                error.context = FailureContext(error.context.stage, None, target)
        raise


def _install_command(
    *,
    python: Path,
    wheel: Path,
    wheel_set: Path,
    policy: ValidationPolicy,
    uv: str = "uv",
) -> tuple[str, ...]:
    install_target = str(wheel)
    if policy.extras:
        install_target += f"[{policy.extras}]"
    return (
        uv,
        "pip",
        "install",
        "--no-config",
        "--no-cache",
        f"--python={python}",
        "--no-index",
        "--only-binary=:all:",
        f"--find-links={wheel_set}",
        install_target,
        *policy.missing_dependencies,
    )


def _uv_environment() -> dict[str, str]:
    return uv_environment()


def _run_validation(
    *,
    metadata: WheelMetadata,
    wheel: Path,
    wheel_set: Path,
    target: Target,
    policy: ValidationPolicy,
    uv: str = "uv",
) -> None:
    if sys.implementation.name != "cpython" or sys.version_info[:2] != tuple(
        target.python_version.release[:2]
    ):
        raise _failure(
            metadata.identity,
            target,
            f"validation requires CPython {target.python_version}; current interpreter is "
            f"{sys.implementation.name} {sys.version_info.major}.{sys.version_info.minor}."
            f"{sys.version_info.micro}",
        )
    local_platform = _local_platform()
    if local_platform != target.platform:
        raise _failure(
            metadata.identity,
            target,
            f"validation requires a native {target.platform} runner; "
            f"current runner is {local_platform or 'unsupported'}",
        )
    uv_environment = _uv_environment()
    python_executable = str(Path(sys.executable).resolve())

    def run(command: tuple[str, ...], stage: str, cwd: Path) -> None:
        try:
            run_process(
                command,
                check=True,
                capture_output=True,
                text=True,
                env=uv_environment,
                cwd=cwd,
                timeout=300,
            )
        except subprocess.TimeoutExpired as cause:
            raise _failure(
                metadata.identity,
                target,
                f"{stage} timed out after 300 seconds",
                cause=cause,
            ) from cause
        except subprocess.CalledProcessError as cause:
            detail = f"{stage} failed for {wheel.name}"
            if cause.stderr:
                detail = f"{detail}: {redacted_diagnostic(cause.stderr)}"
            raise _failure(
                metadata.identity,
                target,
                detail,
                cause=cause,
            )

    try:
        with tempfile.TemporaryDirectory(prefix="mypypi-validate-") as directory:
            neutral_directory = Path(directory).resolve()
            environment = neutral_directory / "venv"
            run(
                (
                    uv,
                    "venv",
                    "--no-config",
                    "--quiet",
                    "--python-preference=only-system",
                    f"--python={python_executable}",
                    str(environment),
                ),
                "environment creation",
                neutral_directory,
            )
            python = environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            command = _install_command(
                python=python,
                wheel=wheel,
                wheel_set=wheel_set,
                policy=policy,
                uv=uv,
            )
            run(command, "dependency installation", neutral_directory)
            for import_name in metadata.imports:
                if import_name in policy.skipped_imports:
                    continue
                run(
                    (str(python), "-I", "-c", f"__import__({import_name!r})"),
                    f"import {import_name}",
                    neutral_directory,
                )
    except MypypiError:
        raise
    except OSError as cause:
        raise _failure(
            metadata.identity,
            target,
            f"validation process failed: {cause}",
            cause=cause,
        ) from cause


def validate_wheel(
    wheel: Path,
    *,
    target: Target,
    wheel_set: Path,
    policy: ValidationPolicy = ValidationPolicy(),
    uv: str = "uv",
    linux_repair_policy: str = "manylinux_2_28",
) -> WheelMetadata:
    try:
        metadata = inspect_wheel(wheel, target, linux_repair_policy)
        if not wheel.is_file():
            raise _failure(metadata.identity, target, f"wheel does not exist: {wheel}")
        _run_validation(
            metadata=metadata,
            wheel=wheel,
            wheel_set=wheel_set,
            target=target,
            policy=policy,
            uv=uv,
        )
        return metadata
    except MypypiError:
        raise
    except ValueError as cause:
        try:
            identity = WheelIdentity.parse(wheel.name)
        except ValueError:
            identity = None
        raise _failure(identity, target, str(cause), cause=cause)


def validate_wheels(
    wheels: tuple[Path, ...],
    *,
    targets: tuple[Target, ...],
    wheel_set: Path,
    policies: dict[PackageVersion, ValidationPolicy] | None = None,
    uv: str = "uv",
    linux_repair_policy: str = "manylinux_2_28",
) -> tuple[WheelMetadata, ...]:
    if not wheels:
        return ()
    if not targets:
        raise ValueError("validation requires at least one target")
    if len(set(targets)) != len(targets):
        raise ValueError("validation targets must be unique")
    metadata: list[WheelMetadata] = []
    policies = policies or {}
    for wheel in sorted(wheels):
        identity = WheelIdentity.parse(wheel.name)
        for target in targets:
            if identity.tags.intersection(_supported_tags(target, linux_repair_policy)):
                metadata.append(
                    validate_wheel(
                        wheel,
                        target=target,
                        wheel_set=wheel_set,
                        policy=policies.get(identity.package, ValidationPolicy()),
                        uv=uv,
                        linux_repair_policy=linux_repair_policy,
                    )
                )
    return tuple(metadata)
