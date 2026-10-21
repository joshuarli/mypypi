from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from packaging.tags import Tag
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version


class ErrorKind(StrEnum):
    INVALID_INPUT = "invalid-input"
    CONFIGURATION = "configuration"
    MISSING_ARTIFACT = "missing-artifact"
    INTEGRITY = "integrity"
    INCOMPATIBLE = "incompatible"
    DEPENDENCY = "dependency"
    BUILD = "build"
    VALIDATION = "validation"
    CONFLICT = "conflict"
    IO = "io"


class FailureStage(StrEnum):
    CONFIGURATION = "configuration"
    PLANNING = "planning"
    ACQUISITION = "acquisition"
    BUILD = "build"
    REPAIR = "repair"
    VALIDATION = "validation"
    PUBLICATION = "publication"
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class PackageVersion:
    name: str
    version: Version

    def __post_init__(self) -> None:
        if canonicalize_name(self.name) != self.name:
            raise ValueError("package name must be canonical")
        if not isinstance(self.version, Version):
            raise TypeError("package version must be a parsed Version")

    @classmethod
    def parse(cls, name: str, version: str) -> PackageVersion:
        return cls(canonicalize_name(name), Version(version))


@dataclass(frozen=True, slots=True)
class Target:
    python_version: Version
    platform: str

    def __post_init__(self) -> None:
        if not self.platform or self.platform.strip() != self.platform:
            raise ValueError("target platform must be a non-empty name")
        if not isinstance(self.python_version, Version):
            raise TypeError("target Python version must be a parsed Version")


@dataclass(frozen=True, slots=True)
class WheelIdentity:
    filename: str
    package: PackageVersion
    build_tag: tuple[int, str] | None
    tags: frozenset[Tag]

    @classmethod
    def parse(cls, filename: str) -> WheelIdentity:
        name, version, build_tag, tags = parse_wheel_filename(filename)
        return cls(
            filename=filename,
            package=PackageVersion(str(name), version),
            build_tag=build_tag or None,
            tags=frozenset(tags),
        )


@dataclass(frozen=True, slots=True)
class ValidatedArtifact:
    identity: WheelIdentity
    path: Path
    sha256: str
    targets: frozenset[Target]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("SHA-256 must contain 64 lowercase hexadecimal characters")
        if not self.targets:
            raise ValueError("validated artifact must cover at least one target")


@dataclass(frozen=True, slots=True)
class AlreadyPublished:
    package: PackageVersion
    target: Target
    wheel: WheelIdentity


@dataclass(frozen=True, slots=True)
class DownloadWheel:
    package: PackageVersion
    target: Target
    wheel: WheelIdentity
    url: str
    sha256: str


@dataclass(frozen=True, slots=True)
class BuildSdist:
    package: PackageVersion
    target: Target
    url: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ExcludedTarget:
    package: PackageVersion
    target: Target
    reason: str


@dataclass(frozen=True, slots=True)
class PendingValidation:
    package: PackageVersion
    target: Target
    wheel: WheelIdentity


@dataclass(frozen=True, slots=True)
class ArtifactConflict:
    package: PackageVersion
    target: Target
    filename: str
    existing_sha256: str
    candidate_sha256: str


PlanOutcome = (
    AlreadyPublished
    | DownloadWheel
    | BuildSdist
    | ExcludedTarget
    | PendingValidation
    | ArtifactConflict
)


@dataclass(frozen=True, slots=True)
class FailureContext:
    stage: FailureStage
    package: PackageVersion | None = None
    target: Target | None = None


class MypypiError(Exception):
    def __init__(self, kind: ErrorKind, detail: str, context: FailureContext):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.context = context
