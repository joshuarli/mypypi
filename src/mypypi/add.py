from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import Version

from .catalog import Catalog, parse_catalog
from .models import (
    ErrorKind,
    FailureContext,
    FailureStage,
    MypypiError,
    PackageVersion,
    Target,
)
from .platforms import target_python_version
from .process_policy import run_process, uv_environment

_PYTHON_PLATFORMS = {
    "linux-x86_64": "x86_64-manylinux_2_28",
    "linux-aarch64": "aarch64-manylinux_2_28",
    "macos-x86_64": "x86_64-apple-darwin",
    "macos-arm64": "aarch64-apple-darwin",
}


@dataclass(frozen=True, slots=True)
class _ResolvedPin:
    package: PackageVersion
    platforms: tuple[str, ...] | None


def _dependency_error(detail: str, cause: Exception | None = None) -> MypypiError:
    error = MypypiError(
        ErrorKind.DEPENDENCY,
        detail,
        FailureContext(FailureStage.PLANNING),
    )
    if cause is not None:
        error.__cause__ = cause
    return error


def _parse_exact_requirements(
    requirements: Sequence[str],
) -> tuple[tuple[PackageVersion, ...], tuple[str, ...]]:
    packages = []
    source = []
    for requirement in requirements:
        try:
            parsed = Requirement(requirement.strip())
        except InvalidRequirement as cause:
            raise MypypiError(
                ErrorKind.INVALID_INPUT,
                f"requirement must be an exact name==version pin: {requirement!r}",
                FailureContext(FailureStage.COMMAND),
            ) from cause
        specifiers = tuple(parsed.specifier)
        if (
            parsed.url is not None
            or parsed.marker is not None
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise MypypiError(
                ErrorKind.INVALID_INPUT,
                f"requirement must be an exact name==version pin: {requirement!r}",
                FailureContext(FailureStage.COMMAND),
            )
        try:
            packages.append(PackageVersion.parse(parsed.name, specifiers[0].version))
        except ValueError as cause:
            raise MypypiError(
                ErrorKind.INVALID_INPUT,
                f"invalid exact pin {requirement!r}",
                FailureContext(FailureStage.COMMAND),
            ) from cause
        source.append(requirement.strip())
    if not packages:
        raise MypypiError(
            ErrorKind.INVALID_INPUT,
            "at least one exact package pin is required",
            FailureContext(FailureStage.COMMAND),
        )
    versions_by_name: dict[str, set[Version]] = {}
    for package in packages:
        versions_by_name.setdefault(package.name, set()).add(package.version)
    for name, versions in versions_by_name.items():
        if len(versions) > 1:
            raise MypypiError(
                ErrorKind.CONFLICT,
                f"conflicting requested pins for {name}: {', '.join(map(str, sorted(versions)))}",
                FailureContext(FailureStage.COMMAND),
            )
    return tuple(dict.fromkeys(packages)), tuple(source)


def parse_pins(requirements: Sequence[str]) -> tuple[PackageVersion, ...]:
    packages, _ = _parse_exact_requirements(requirements)
    return packages


def _parse_compiled(output: str) -> tuple[PackageVersion, ...]:
    resolved = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as cause:
            raise _dependency_error(
                f"uv returned an invalid pinned requirement: {line!r}", cause
            ) from cause
        specifiers = tuple(requirement.specifier)
        if (
            requirement.url is not None
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise _dependency_error(
                f"uv returned an invalid pinned requirement: {line!r}"
            )
        try:
            resolved.append(
                PackageVersion.parse(requirement.name, specifiers[0].version)
            )
        except ValueError as cause:
            raise _dependency_error(
                f"uv returned an invalid pinned requirement: {line!r}", cause
            ) from cause
    return tuple(resolved)


def compile_for_target(
    requirements: Sequence[str],
    target: Target,
    python: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_process,
) -> tuple[PackageVersion, ...]:
    platform = _PYTHON_PLATFORMS.get(target.platform)
    if platform is None:
        raise _dependency_error(f"unsupported resolution platform: {target.platform}")
    command = [
        "uv",
        "pip",
        "compile",
        "--no-config",
        "--quiet",
        "--no-header",
        "--no-annotate",
        f"--python={python}",
        f"--python-version={target_python_version(target)}",
        f"--python-platform={platform}",
        "--default-index=https://pypi.org/simple",
        "-",
    ]
    try:
        result = run(
            command,
            input="\n".join(requirements),
            text=True,
            capture_output=True,
            check=True,
            env=uv_environment(),
            timeout=300,
        )
    except subprocess.TimeoutExpired as cause:
        raise _dependency_error(
            f"uv dependency resolution timed out for {target.platform}", cause
        ) from cause
    except (OSError, subprocess.CalledProcessError) as cause:
        raise _dependency_error(
            f"uv dependency resolution failed for {target.platform}", cause
        ) from cause
    return _parse_compiled(result.stdout)


def resolve_dependencies(
    requirements: Sequence[str],
    targets: Sequence[Target],
    python: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_process,
) -> tuple[PackageVersion, ...]:
    return tuple(
        pin.package
        for pin in _resolve_dependencies(requirements, targets, python, run)
    )


def _resolve_dependencies(
    requirements: Sequence[str],
    targets: Sequence[Target],
    python: str,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[_ResolvedPin, ...]:
    pins, normalized_requirements = _parse_exact_requirements(requirements)
    resolutions: list[tuple[Target, tuple[PackageVersion, ...]]] = []
    for target in targets:
        resolution = compile_for_target(normalized_requirements, target, python, run)
        by_name: dict[str, set[Version]] = {}
        for package in resolution:
            by_name.setdefault(package.name, set()).add(package.version)
        conflicts = {
            name: found for name, found in by_name.items() if len(found) > 1
        }
        if conflicts:
            name, found = sorted(conflicts.items())[0]
            package = PackageVersion(name, min(found))
            raise MypypiError(
                ErrorKind.CONFLICT,
                f"dependency resolution conflicts for {name} on {target.platform}: "
                f"{', '.join(map(str, sorted(found)))}",
                FailureContext(FailureStage.PLANNING, package, target),
            )
        resolved_by_name = {
            package.name: package.version for package in resolution
        }
        for pin in pins:
            resolved = resolved_by_name.get(pin.name)
            if resolved is not None and resolved != pin.version:
                raise MypypiError(
                    ErrorKind.CONFLICT,
                    f"resolved dependency conflicts with requested pin "
                    f"{pin.name}=={pin.version} on {target.platform}: {resolved}",
                    FailureContext(FailureStage.PLANNING, pin, target),
                )
        resolutions.append((target, resolution))

    root_names = {pin.name for pin in pins}
    transitive_targets: dict[PackageVersion, list[str]] = {}
    for target, resolution in resolutions:
        for package in resolution:
            if package.name in root_names:
                continue
            platforms = transitive_targets.setdefault(package, [])
            if target.platform not in platforms:
                platforms.append(target.platform)

    resolved = [_ResolvedPin(pin, None) for pin in pins]
    resolved.extend(
        _ResolvedPin(package, tuple(platforms))
        for package, platforms in transitive_targets.items()
    )
    return tuple(
        sorted(
            resolved,
            key=lambda pin: (pin.package.name, pin.package.version),
        )
    )


def append_entries(
    source: str,
    packages: Sequence[PackageVersion | _ResolvedPin],
    existing: Catalog,
) -> str:
    present = {entry.package: entry for entry in existing.entries}
    requested: dict[PackageVersion, set[str] | None] = {}
    for item in packages:
        package = item if isinstance(item, PackageVersion) else item.package
        platforms = None if isinstance(item, PackageVersion) else item.platforms
        if package not in requested:
            requested[package] = None if platforms is None else set(platforms)
        elif requested[package] is not None:
            if platforms is None:
                requested[package] = None
            else:
                requested[package].update(platforms)
    additions: list[tuple[PackageVersion, tuple[str, ...] | None]] = []
    for package, platforms in requested.items():
        current = present.get(package)
        if current is not None and current.platforms is None:
            continue
        if platforms is None:
            additions.append((package, None))
            continue
        current_platforms = set(current.platforms) if current is not None else set()
        missing = tuple(
            platform for platform in platforms if platform not in current_platforms
        )
        if missing:
            additions.append((package, missing))
    if not additions:
        return source
    source = re.sub(r"(?m)^packages\s*=\s*\[\]\s*(?=#|$)", "", source, count=1)
    separator = "" if not source or source.endswith("\n") else "\n"
    rows = []
    for package, platforms in additions:
        row = (
            f'\n[[packages]]\nname = "{package.name}"\n'
            f'version = "{package.version}"\n'
        )
        if platforms is not None:
            row += f"platforms = {json.dumps(platforms)}\n"
        rows.append(row)
    updated = f"{source}{separator}{''.join(rows)}"
    parse_catalog(updated)
    return updated


def add_packages(
    path: Path,
    requirements: Sequence[str],
    targets: Sequence[Target],
    python: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_process,
) -> tuple[PackageVersion, ...]:
    initial = _read_catalog(path)
    parse_catalog(initial)
    resolved = _resolve_dependencies(requirements, targets, python, run)
    with _CatalogLock(path):
        source = _read_catalog(path)
        existing = parse_catalog(source)
        updated = append_entries(source, resolved, existing)
        parse_catalog(updated)
        if updated != source:
            _replace_catalog(path, updated)
    return tuple(pin.package for pin in resolved)


def _read_catalog(path: Path) -> str:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        mode = os.fstat(descriptor).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            os.close(descriptor)
            descriptor = None
            raise MypypiError(
                ErrorKind.INVALID_INPUT,
                f"catalog path must be a regular file: {path}",
                FailureContext(FailureStage.CONFIGURATION),
            )
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as catalog_file:
            descriptor = None
            return catalog_file.read()
    except MypypiError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as cause:
        if descriptor is not None:
            os.close(descriptor)
        raise MypypiError(
            ErrorKind.IO,
            f"could not read catalog at {path}",
            FailureContext(FailureStage.CONFIGURATION),
        ) from cause
    except UnicodeError as cause:
        if descriptor is not None:
            os.close(descriptor)
        raise MypypiError(
            ErrorKind.CONFIGURATION,
            f"catalog at {path} is not valid UTF-8",
            FailureContext(FailureStage.CONFIGURATION),
        ) from cause


class _CatalogLock:
    def __init__(self, path: Path):
        self.path = path.with_name(f".{path.name}.lock")
        self.descriptor: int | None = None

    def __enter__(self) -> _CatalogLock:
        try:
            self.descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        except OSError as cause:
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None
            raise MypypiError(
                ErrorKind.IO,
                f"could not lock catalog at {self.path}",
                FailureContext(FailureStage.CONFIGURATION),
            ) from cause
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        if self.descriptor is None:
            return
        descriptor = self.descriptor
        self.descriptor = None
        cleanup_errors = []
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError as cause:
            cleanup_errors.append(cause)
        try:
            os.close(descriptor)
        except OSError as cause:
            cleanup_errors.append(cause)
        if exc is not None:
            for cause in cleanup_errors:
                exc.add_note(f"could not release catalog lock {self.path}: {cause}")
        elif cleanup_errors:
            raise MypypiError(
                ErrorKind.IO,
                f"could not release catalog lock at {self.path}",
                FailureContext(FailureStage.CONFIGURATION),
            ) from cleanup_errors[0]


def _replace_catalog(path: Path, source: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    failure: BaseException | None = None
    try:
        source_mode = path.lstat().st_mode
        if stat.S_ISLNK(source_mode) or not stat.S_ISREG(source_mode):
            raise OSError(f"catalog path is not a regular file: {path}")
        mode = stat.S_IMODE(source_mode)
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            mode,
        )
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(source.encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except OSError as cause:
        failure = MypypiError(
            ErrorKind.IO,
            f"could not update catalog at {path}",
            FailureContext(FailureStage.CONFIGURATION),
        )
        raise failure from cause
    except BaseException as cause:
        failure = cause
        raise
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as cause:
            if failure is None:
                raise MypypiError(
                    ErrorKind.IO,
                    f"could not remove temporary catalog file {temporary}",
                    FailureContext(FailureStage.CONFIGURATION),
                ) from cause
            failure.add_note(f"could not remove temporary catalog file {temporary}: {cause}")
