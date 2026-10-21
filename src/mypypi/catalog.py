from __future__ import annotations

import tomllib
from dataclasses import dataclass
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import Version

from .models import (
    ErrorKind,
    FailureContext,
    FailureStage,
    MypypiError,
    PackageVersion,
)


@dataclass(frozen=True, slots=True)
class BuildSettings:
    likely_binary_ignore: tuple[tuple[str, str], ...] = ()
    custom_prebuild: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationSettings:
    validate_extras: tuple[tuple[str, str], ...] = ()
    validate_incorrect_missing_deps: tuple[tuple[str, str], ...] = ()
    validate_skip_imports: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class PackageEntry:
    package: PackageVersion
    build: BuildSettings
    validation: ValidationSettings
    excluded_platforms: tuple[tuple[str, str], ...]
    yanked_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Catalog:
    entries: tuple[PackageEntry, ...]

    def by_package(self) -> dict[PackageVersion, PackageEntry]:
        return {entry.package: entry for entry in self.entries}


def _error(detail: str) -> MypypiError:
    return MypypiError(
        ErrorKind.CONFIGURATION,
        detail,
        FailureContext(FailureStage.CONFIGURATION),
    )


def _mapping(value: Any, label: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(f"{label} must be a table")
    unknown = set(value) - allowed
    if unknown:
        raise _error(f"unknown {label} key: {sorted(unknown)[0]}")
    return value


def _reason_map(value: Any, label: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        raise _error(f"{label} must map each exception to a reason")
    pairs = []
    for key, reason in value.items():
        if not isinstance(key, str) or not key.strip():
            raise _error(f"{label} keys must be non-empty strings")
        if not isinstance(reason, str) or not reason.strip():
            raise _error(f"{label} requires a non-empty reason for {key}")
        pairs.append((key, reason))
    return tuple(sorted(pairs))



def _entry(value: Any) -> PackageEntry:
    row = _mapping(
        value,
        "package entry",
        {
            "name",
            "version",
            "build",
            "validation",
            "exclude_platforms",
            "yanked_reason",
        },
    )
    name = row.get("name")
    version_text = row.get("version")
    if not isinstance(name, str) or not name or canonicalize_name(name) != name:
        raise _error("package name must be canonical")
    if not isinstance(version_text, str) or not version_text:
        raise _error(f"{name}: version must be an exact version pin")
    try:
        version = Version(version_text)
    except ValueError as cause:
        raise _error(f"{name}: version must be an exact canonical version") from cause
    if str(version) != version_text:
        raise _error(f"{name}: version pin must be canonical")
    package = PackageVersion(name, version)

    build_row = _mapping(
        row.get("build", {}),
        f"{package} build settings",
        {"likely_binary_ignore", "custom_prebuild"},
    )
    likely_binary_ignore = _reason_map(
        build_row.get("likely_binary_ignore", {}),
        f"{package} likely_binary_ignore",
    )
    custom_prebuild = _reason_map(
        build_row.get("custom_prebuild", {}), f"{package} custom_prebuild"
    )
    if custom_prebuild and not all(
        "{prefix}" in command for command, _ in custom_prebuild
    ):
        raise _error(f"{package}: every custom_prebuild command must use {{prefix}}")

    validation_row = _mapping(
        row.get("validation", {}),
        f"{package} validation settings",
        {
            "validate_extras",
            "validate_incorrect_missing_deps",
            "validate_skip_imports",
        },
    )
    validation = ValidationSettings(
        validate_extras=_reason_map(
            validation_row.get("validate_extras", {}),
            f"{package} validate_extras",
        ),
        validate_incorrect_missing_deps=_reason_map(
            validation_row.get("validate_incorrect_missing_deps", {}),
            f"{package} validate_incorrect_missing_deps",
        ),
        validate_skip_imports=_reason_map(
            validation_row.get("validate_skip_imports", {}),
            f"{package} validate_skip_imports",
        ),
    )
    exclusions = _reason_map(row.get("exclude_platforms", {}), f"{package} exclusions")
    yanked_reason = row.get("yanked_reason")
    if yanked_reason is not None and (
        not isinstance(yanked_reason, str) or not yanked_reason.strip()
    ):
        raise _error(f"{package}: yanked_reason must be non-empty")
    return PackageEntry(
        package,
        BuildSettings(likely_binary_ignore, custom_prebuild),
        validation,
        exclusions,
        yanked_reason,
    )


def parse_catalog(source: str) -> Catalog:
    try:
        document = tomllib.loads(source)
    except tomllib.TOMLDecodeError as cause:
        raise _error("invalid packages.toml") from cause
    unknown = set(document) - {"packages"}
    if unknown:
        raise _error(f"unknown catalog key: {sorted(unknown)[0]}")
    if "packages" not in document:
        raise _error("packages must be an array of tables")
    rows = document["packages"]
    if not isinstance(rows, list):
        raise _error("packages must be an array of tables")
    entries = tuple(_entry(row) for row in rows)
    identities = [entry.package for entry in entries]
    if len(set(identities)) != len(identities):
        raise _error("catalog contains a duplicate package version")
    return Catalog(entries)
