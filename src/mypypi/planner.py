from __future__ import annotations

from dataclasses import dataclass

from .catalog import Catalog
from .models import (
    AlreadyPublished,
    ArtifactConflict,
    BuildSdist,
    DownloadWheel,
    ErrorKind,
    ExcludedTarget,
    FailureContext,
    FailureStage,
    MypypiError,
    PackageVersion,
    PendingValidation,
    PlanOutcome,
    Target,
    WheelIdentity,
)


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    wheel: WheelIdentity
    sha256: str


@dataclass(frozen=True, slots=True)
class UpstreamWheel:
    wheel: WheelIdentity
    url: str
    sha256: str


@dataclass(frozen=True, slots=True)
class UpstreamSdist:
    url: str
    sha256: str


type UpstreamArtifact = UpstreamWheel | UpstreamSdist
type ArtifactKey = tuple[PackageVersion, Target]


def validate_catalog_targets(
    catalog: Catalog, configured_targets: tuple[Target, ...]
) -> None:
    configured_platforms = {target.platform for target in configured_targets}
    for entry in catalog.entries:
        unknown_platforms = set(dict(entry.excluded_platforms)) - configured_platforms
        unknown_applicability = set(entry.platforms or ()) - configured_platforms
        if unknown_platforms:
            raise MypypiError(
                ErrorKind.CONFIGURATION,
                "unsupported excluded platform for "
                f"{entry.package.name}=={entry.package.version}: "
                f"{sorted(unknown_platforms)[0]}",
                FailureContext(FailureStage.CONFIGURATION, entry.package),
            )
        if unknown_applicability:
            raise MypypiError(
                ErrorKind.CONFIGURATION,
                "unsupported target platform for "
                f"{entry.package.name}=={entry.package.version}: "
                f"{sorted(unknown_applicability)[0]}",
                FailureContext(FailureStage.CONFIGURATION, entry.package),
            )


def plan(
    catalog: Catalog,
    targets: tuple[Target, ...],
    published: dict[ArtifactKey, ArtifactRecord],
    upstream: dict[ArtifactKey, tuple[UpstreamArtifact, ...]],
    pending_validation: dict[ArtifactKey, ArtifactRecord] | None = None,
    configured_targets: tuple[Target, ...] | None = None,
) -> tuple[PlanOutcome, ...]:
    pending_validation = pending_validation or {}
    outcomes: list[PlanOutcome] = []
    local_artifacts = (*published.values(), *pending_validation.values())
    local_filenames: dict[str, ArtifactRecord] = {
        artifact.wheel.filename: artifact for artifact in local_artifacts
    }
    if configured_targets is None:
        configured_targets = targets
    validate_catalog_targets(catalog, configured_targets)
    for entry in catalog.entries:
        exclusions = dict(entry.excluded_platforms)
        for target in targets:
            if entry.platforms is not None and target.platform not in entry.platforms:
                continue
            key = (entry.package, target)
            reason = exclusions.get(target.platform)
            if reason is not None:
                outcomes.append(ExcludedTarget(entry.package, target, reason))
                continue
            current = published.get(key)
            if current is not None:
                outcomes.append(AlreadyPublished(entry.package, target, current.wheel))
                continue
            pending = pending_validation.get(key)
            if pending is not None:
                existing = local_filenames.get(pending.wheel.filename)
                if existing is not None and existing.sha256 != pending.sha256:
                    outcomes.append(
                        ArtifactConflict(
                            entry.package,
                            target,
                            pending.wheel.filename,
                            existing.sha256,
                            pending.sha256,
                        )
                    )
                else:
                    outcomes.append(
                        PendingValidation(entry.package, target, pending.wheel)
                    )
                continue
            candidates = upstream.get(key, ())
            wheels = [item for item in candidates if isinstance(item, UpstreamWheel)]
            if wheels:
                candidate = wheels[0]
                existing = local_filenames.get(candidate.wheel.filename)
                if existing is not None and existing.sha256 != candidate.sha256:
                    outcomes.append(
                        ArtifactConflict(
                            entry.package,
                            target,
                            candidate.wheel.filename,
                            existing.sha256,
                            candidate.sha256,
                        )
                    )
                else:
                    outcomes.append(
                        DownloadWheel(
                            entry.package,
                            target,
                            candidate.wheel,
                            candidate.url,
                            candidate.sha256,
                        )
                    )
                continue
            sdists = [item for item in candidates if isinstance(item, UpstreamSdist)]
            if sdists:
                candidate = sdists[0]
                outcomes.append(
                    BuildSdist(entry.package, target, candidate.url, candidate.sha256)
                )
                continue
            raise MypypiError(
                ErrorKind.MISSING_ARTIFACT,
                "no compatible upstream wheel or source archive for "
                f"{entry.package.name}=={entry.package.version} on {target.platform}",
                FailureContext(FailureStage.PLANNING, entry.package, target),
            )
    return tuple(outcomes)
