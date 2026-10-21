from __future__ import annotations

import fcntl
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

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

_PYTHON_PLATFORMS = {
    "linux-x86_64": "x86_64-unknown-linux-gnu",
    "linux-aarch64": "aarch64-unknown-linux-gnu",
    "macos-x86_64": "x86_64-apple-darwin",
    "macos-arm64": "aarch64-apple-darwin",
}
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^=<>!~\s]+)$")


def _dependency_error(detail: str, cause: Exception | None = None) -> MypypiError:
    error = MypypiError(
        ErrorKind.DEPENDENCY,
        detail,
        FailureContext(FailureStage.PLANNING),
    )
    if cause is not None:
        error.__cause__ = cause
    return error


def parse_pins(requirements: Sequence[str]) -> tuple[PackageVersion, ...]:
    packages = []
    for requirement in requirements:
        match = _PIN.fullmatch(requirement.strip())
        if match is None:
            raise MypypiError(
                ErrorKind.INVALID_INPUT,
                f"requirement must be an exact name==version pin: {requirement!r}",
                FailureContext(FailureStage.COMMAND),
            )
        try:
            packages.append(PackageVersion.parse(match.group(1), match.group(2)))
        except ValueError as cause:
            raise MypypiError(
                ErrorKind.INVALID_INPUT,
                f"invalid exact pin {requirement!r}",
                FailureContext(FailureStage.COMMAND),
            ) from cause
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
    return tuple(dict.fromkeys(packages))


def _parse_compiled(output: str) -> tuple[PackageVersion, ...]:
    resolved = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.fullmatch(line)
        if match is None:
            raise _dependency_error(
                f"uv returned an invalid pinned requirement: {line!r}"
            )
        try:
            resolved.append(PackageVersion.parse(match.group(1), match.group(2)))
        except ValueError as cause:
            raise _dependency_error(
                f"uv returned an invalid pinned requirement: {line!r}", cause
            ) from cause
    return tuple(resolved)


def compile_for_target(
    requirements: Sequence[str],
    target: Target,
    python: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
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
        f"--python-version={target.python_version}",
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
        )
    except (OSError, subprocess.CalledProcessError) as cause:
        raise _dependency_error(
            f"uv dependency resolution failed for {target.platform}", cause
        ) from cause
    return _parse_compiled(result.stdout)


def resolve_dependencies(
    requirements: Sequence[str],
    targets: Sequence[Target],
    python: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[PackageVersion, ...]:
    pins = parse_pins(requirements)
    resolutions = [
        compile_for_target(requirements, target, python, run) for target in targets
    ]
    versions: dict[str, set[str]] = {}
    for resolution in resolutions:
        for package in resolution:
            versions.setdefault(package.name, set()).add(str(package.version))
    conflicts = {name: found for name, found in versions.items() if len(found) > 1}
    if conflicts:
        name, found = sorted(conflicts.items())[0]
        raise MypypiError(
            ErrorKind.CONFLICT,
            "dependency resolution conflicts across configured platforms for "
            f"{name}: {', '.join(sorted(found))}",
            FailureContext(FailureStage.PLANNING),
        )
    for pin in pins:
        found = versions.get(pin.name, set())
        if found and found != {str(pin.version)}:
            raise MypypiError(
                ErrorKind.CONFLICT,
                f"resolved dependency conflicts with requested pin {pin.name}=={pin.version}",
                FailureContext(FailureStage.PLANNING, package=pin),
            )
    for pin in pins:
        versions.setdefault(pin.name, {str(pin.version)})
    return tuple(
        PackageVersion(name, Version(next(iter(found))))
        for name, found in sorted(versions.items())
    )


def append_entries(
    source: str, packages: Sequence[PackageVersion], existing: Catalog
) -> str:
    present = {entry.package for entry in existing.entries}
    additions = [package for package in packages if package not in present]
    if not additions:
        return source
    source = re.sub(r"(?m)^packages\s*=\s*\[\]\s*(?=#|$)", "", source, count=1)
    separator = "" if not source or source.endswith("\n") else "\n"
    rows = "".join(
        f'\n[[packages]]\nname = "{package.name}"\nversion = "{package.version}"\n'
        for package in additions
    )
    return f"{source}{separator}{rows}"


def add_packages(
    path: Path,
    requirements: Sequence[str],
    targets: Sequence[Target],
    python: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[PackageVersion, ...]:
    initial = _read_catalog(path)
    parse_catalog(initial)
    resolved = resolve_dependencies(requirements, targets, python, run)
    with _CatalogLock(path):
        source = _read_catalog(path)
        existing = parse_catalog(source)
        updated = append_entries(source, resolved, existing)
        if updated != source:
            _replace_catalog(path, updated)
    return resolved


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
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as cause:
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None
            raise MypypiError(
                ErrorKind.CONFLICT,
                "another add command is updating the catalog",
                FailureContext(FailureStage.CONFIGURATION),
            ) from cause
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
