from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import sys
import sysconfig
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from packaging.version import Version

from .acquire import (
    CompatibilityRejection,
    Release,
    compatible_wheels,
    fetch_release,
    validate_wheel_architectures,
    validate_wheel_metadata,
)
from .add import add_packages
from .build import acquire_or_build, require_native_target, validate_build_index
from .catalog import Catalog, PackageEntry, parse_catalog
from .config import ProjectSettings, load_project_settings
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
    ValidatedArtifact,
    WheelIdentity,
)
from .planner import (
    ArtifactKey,
    ArtifactRecord,
    UpstreamSdist,
    UpstreamWheel,
    plan,
    validate_catalog_targets,
)
from .platforms import ordered_target_tags, target_python_version
from .store import publish
from .store import published_artifacts as read_published_artifacts
from .validate import (
    ExceptionKind,
    ValidationException,
    ValidationPolicy,
    _local_platform,
    inspect_wheel,
    validate_wheels,
)

_DEFAULT_BUILD_INDEX = "https://pypi.org/simple"


def _runtime_description(settings: ProjectSettings) -> str:
    actual = Version(
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    if sys.implementation.name != "cpython" or not settings.python_specifier.contains(
        str(actual), prereleases=True
    ):
        raise _error(
            ErrorKind.CONFIGURATION,
            f"mypypi requires CPython {settings.python_version}; running "
            f"{sys.implementation.name} {actual}",
            FailureStage.CONFIGURATION,
        )
    abi = sys.implementation.cache_tag
    platform_tag = sysconfig.get_platform()
    return (
        f"using CPython {actual} ABI {abi} on {platform_tag}; "
        f"executable {Path(sys.executable).resolve()}"
    )
def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mypypi",
        description="Build and publish validated Python wheels to a local index.",
    )
    parser.add_argument("--catalog", type=Path, default=Path("packages.toml"))
    parser.add_argument("--state-dir", type=Path, default=Path(".mypypi"))
    parser.add_argument("--config", type=Path, default=Path("pyproject.toml"))
    commands = parser.add_subparsers(dest="command")

    add = commands.add_parser("add", help="resolve package requirements into the catalog")
    add.add_argument("requirements", nargs="+")

    plan_command = commands.add_parser("plan", help="show the wheel work required by the catalog")
    plan_command.add_argument("--output", type=Path, default=Path("dist"))
    _add_platform_arguments(plan_command)

    build = commands.add_parser("build", help="acquire or build missing wheels")
    build.add_argument("--output", type=Path, default=Path("dist"))
    build.add_argument("--build-index", default=_DEFAULT_BUILD_INDEX)
    build.add_argument("--strict-build-deps", action="store_true")
    build.add_argument("--build-constraints", type=Path)
    build.add_argument("--require-build-hashes", action="store_true")
    _add_platform_arguments(build)

    validate = commands.add_parser("validate", help="validate pending wheels")
    validate.add_argument("--output", type=Path, default=Path("dist"))
    _add_platform_arguments(validate)

    publish_command = commands.add_parser("publish", help="publish validated wheels to a local index")
    publish_command.add_argument("--output", type=Path, required=True)
    _add_platform_arguments(publish_command)
    return parser


def _add_platform_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--platform",
        action="append",
        help="select a configured platform; repeat to select more than one",
    )


def _error(
    kind: ErrorKind,
    detail: str,
    stage: FailureStage,
    package: PackageVersion | None = None,
    target: Target | None = None,
) -> MypypiError:
    return MypypiError(kind, detail, FailureContext(stage, package, target))


def _load_catalog(path: Path) -> Catalog:
    try:
        return parse_catalog(path.read_text(encoding="utf-8"))
    except OSError as cause:
        raise _error(
            ErrorKind.IO,
            f"could not read catalog at {path}",
            FailureStage.CONFIGURATION,
        ) from cause


def _selected_targets(
    settings: ProjectSettings, requested: Sequence[str] | None
) -> tuple[Target, ...]:
    if requested is None:
        return settings.targets()
    if len(set(requested)) != len(requested):
        raise _error(
            ErrorKind.INVALID_INPUT,
            "--platform values must be unique",
            FailureStage.COMMAND,
        )
    configured = dict((target.platform, target) for target in settings.targets())
    unknown = set(requested) - configured.keys()
    if unknown:
        raise _error(
            ErrorKind.INVALID_INPUT,
            f"unconfigured platform: {sorted(unknown)[0]}",
            FailureStage.COMMAND,
        )
    return tuple(configured[name] for name in requested)


def _real_directory(path: Path, *, create: bool) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if not create:
            return False
        try:
            path.mkdir(parents=True)
            mode = path.lstat().st_mode
        except OSError as cause:
            raise _error(
                ErrorKind.IO,
                f"could not create directory {path}",
                FailureStage.COMMAND,
            ) from cause
    except OSError as cause:
        raise _error(
            ErrorKind.IO,
            f"could not inspect directory {path}",
            FailureStage.COMMAND,
        ) from cause
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise _error(
            ErrorKind.INVALID_INPUT,
            f"path must be a real directory: {path}",
            FailureStage.COMMAND,
        )
    return True


def _wheel_paths(state_dir: Path) -> tuple[Path, ...]:
    if not _real_directory(state_dir, create=False):
        return ()
    wheel_dir = state_dir / "wheels"
    if not _real_directory(wheel_dir, create=False):
        return ()
    paths = []
    for path in sorted(wheel_dir.glob("*.whl")):
        try:
            mode = path.lstat().st_mode
        except OSError as cause:
            raise _error(
                ErrorKind.IO,
                f"could not inspect wheel {path}",
                FailureStage.COMMAND,
            ) from cause
        if not stat.S_ISREG(mode):
            raise _error(
                ErrorKind.INVALID_INPUT,
                f"wheel path must be a regular file: {path}",
                FailureStage.COMMAND,
            )
        try:
            WheelIdentity.parse(path.name)
        except ValueError as cause:
            raise _error(
                ErrorKind.INTEGRITY,
                f"invalid wheel filename {path.name}",
                FailureStage.COMMAND,
            ) from cause
        paths.append(path)
    return tuple(paths)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(64 * 1024):
                digest.update(chunk)
    except OSError as cause:
        raise _error(
            ErrorKind.IO,
            f"could not read wheel {path}",
            FailureStage.COMMAND,
        ) from cause
    return digest.hexdigest()


def _published_inventory(
    output: Path, settings: ProjectSettings
) -> dict[ArtifactKey, ArtifactRecord]:
    try:
        root_mode = output.lstat().st_mode
    except FileNotFoundError:
        return {}
    except OSError as cause:
        raise _error(
            ErrorKind.IO,
            f"could not inspect published index at {output}",
            FailureStage.PLANNING,
        ) from cause
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise _error(
            ErrorKind.INVALID_INPUT,
            f"published index root must be a real directory: {output}",
            FailureStage.PLANNING,
        )
    current = output / "current"
    if not current.is_symlink():
        if current.exists():
            raise _error(
                ErrorKind.INTEGRITY,
                f"published index current path is not a symlink: {current}",
                FailureStage.PLANNING,
            )
        return {}
    try:
        link = Path(current.readlink())
    except OSError as cause:
        raise _error(
            ErrorKind.IO,
            f"could not read published index generation at {current}",
            FailureStage.PLANNING,
        ) from cause
    if (
        link.is_absolute()
        or len(link.parts) != 2
        or link.parts[0] != "generations"
        or re.fullmatch(r"[0-9a-f]{64}", link.parts[1]) is None
    ):
        raise _error(
            ErrorKind.INTEGRITY,
            f"published index has an invalid current link: {current}",
            FailureStage.PLANNING,
        )
    generation = output / link
    try:
        generations_mode = (output / "generations").lstat().st_mode
        if stat.S_ISLNK(generations_mode) or not stat.S_ISDIR(generations_mode):
            raise OSError("generations is not a real directory")
        generation_mode = generation.lstat().st_mode
        if stat.S_ISLNK(generation_mode) or not stat.S_ISDIR(generation_mode):
            raise OSError("generation is not a real directory")
        manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
        wheel_dir = output / "wheels"
    except (OSError, UnicodeError, json.JSONDecodeError) as cause:
        raise _error(
            ErrorKind.INTEGRITY,
            f"could not read published index manifest at {generation}",
            FailureStage.PLANNING,
        ) from cause
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "artifacts"}
        or manifest["schema_version"] != 1
    ):
        raise _error(
            ErrorKind.INTEGRITY,
            f"published index manifest has an unknown schema: {generation}",
            FailureStage.PLANNING,
        )
    records = manifest["artifacts"]
    if not isinstance(records, list):
        raise _error(
            ErrorKind.INTEGRITY,
            f"published index manifest has no artifact list: {generation}",
            FailureStage.PLANNING,
        )
    inventory: dict[ArtifactKey, ArtifactRecord] = {}
    known_targets = {target.platform: target for target in settings.targets()}
    for row in records:
        try:
            expected_fields = {
                "filename",
                "name",
                "version",
                "sha256",
                "metadata_sha256",
                "requires_python",
                "yanked",
                "targets",
            }
            if not isinstance(row, dict) or set(row) != expected_fields:
                raise ValueError("manifest artifact has unknown or missing fields")
            filename = row["filename"]
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise ValueError("invalid wheel filename in manifest")
            identity = WheelIdentity.parse(filename)
            if (
                row["name"] != identity.package.name
                or row["version"] != str(identity.package.version)
            ):
                raise ValueError("manifest package identity does not match its wheel")
            checksum = row["sha256"]
            if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
                raise ValueError("invalid wheel hash in manifest")
            metadata_checksum = row["metadata_sha256"]
            if not isinstance(metadata_checksum, str) or re.fullmatch(
                r"[0-9a-f]{64}", metadata_checksum
            ) is None:
                raise ValueError("invalid metadata hash in manifest")
            requires_python = row["requires_python"]
            yanked = row["yanked"]
            if requires_python is not None and not isinstance(requires_python, str):
                raise ValueError("invalid Requires-Python in manifest")
            if yanked is not None and (
                not isinstance(yanked, str) or not yanked.strip()
            ):
                raise ValueError("invalid yanked reason in manifest")
            wheel_dir_mode = wheel_dir.lstat().st_mode
            if stat.S_ISLNK(wheel_dir_mode) or not stat.S_ISDIR(wheel_dir_mode):
                raise ValueError("published wheel storage is not a real directory")
            stored_wheel = wheel_dir / filename
            stored_mode = stored_wheel.lstat().st_mode
            if not stat.S_ISREG(stored_mode) or _sha256(stored_wheel) != checksum:
                raise ValueError(f"published wheel is missing or changed: {filename}")
            sidecar = wheel_dir / f"{filename}.metadata"
            sidecar_mode = sidecar.lstat().st_mode
            if stat.S_ISLNK(sidecar_mode) or not stat.S_ISREG(sidecar_mode):
                raise ValueError(f"published wheel metadata is missing or invalid: {filename}")
            if hashlib.sha256(sidecar.read_bytes()).hexdigest() != metadata_checksum:
                raise ValueError(f"published wheel metadata changed: {filename}")
            target_rows = row["targets"]
            if (
                not isinstance(target_rows, list)
                or not target_rows
                or not all(isinstance(value, str) for value in target_rows)
                or len(set(target_rows)) != len(target_rows)
            ):
                raise ValueError("invalid targets in manifest")
            for value in target_rows:
                python_version_text, separator, platform_name = value.partition(":")
                if not separator or Version(python_version_text) != settings.python_version:
                    raise ValueError("manifest targets do not match project Python")
                target = known_targets.get(platform_name)
                if target is None:
                    raise ValueError(f"manifest target is not configured: {platform_name}")
                preferred_tags = ordered_target_tags(
                    target, settings.linux_repair_policy
                )
                preference = {
                    tag: index for index, tag in enumerate(preferred_tags)
                }
                rank = min(
                    (preference[tag] for tag in identity.tags if tag in preference),
                    default=None,
                )
                if rank is None:
                    raise ValueError(
                        f"published wheel tags do not cover recorded target {target.platform}"
                    )
                key = (identity.package, target)
                record = ArtifactRecord(identity, checksum)
                existing = inventory.get(key)
                if existing is None:
                    inventory[key] = record
                else:
                    existing_rank = min(
                        (preference[tag] for tag in existing.wheel.tags if tag in preference),
                        default=None,
                    )
                    if existing_rank is None:
                        raise ValueError(
                            f"published wheel tags do not cover recorded target {target.platform}"
                        )
                    if (rank, identity.filename) < (existing_rank, existing.wheel.filename):
                        inventory[key] = record
        except (KeyError, TypeError, ValueError, OSError) as cause:
            raise _error(
                ErrorKind.INTEGRITY,
                f"invalid published artifact record: {cause}",
                FailureStage.PLANNING,
            ) from cause
    return inventory


def _published_artifacts(
    output: Path, settings: ProjectSettings
) -> tuple[ValidatedArtifact, ...]:
    return read_published_artifacts(output)


def _pending_inventory(
    paths: tuple[Path, ...],
    catalog: Catalog,
    targets: tuple[Target, ...],
    settings: ProjectSettings,
    published: dict[ArtifactKey, ArtifactRecord],
) -> tuple[dict[ArtifactKey, ArtifactRecord], dict[str, Path]]:
    entries = catalog.by_package()
    candidates: dict[ArtifactKey, list[tuple[int, ArtifactRecord, Path]]] = {}
    local_filenames: dict[str, ArtifactRecord] = {}
    local_paths: dict[str, Path] = {}
    for path in paths:
        identity = WheelIdentity.parse(path.name)
        if identity.package not in entries:
            raise _error(
                ErrorKind.INVALID_INPUT,
                f"wheel is not listed in the catalog: {identity.package.name}=={identity.package.version}",
                FailureStage.PLANNING,
                identity.package,
            )
        record = ArtifactRecord(identity, _sha256(path))
        previous = local_filenames.get(identity.filename)
        if previous is not None and previous.sha256 != record.sha256:
            raise _error(
                ErrorKind.CONFLICT,
                f"wheel filename has conflicting bytes: {identity.filename}",
                FailureStage.PLANNING,
                identity.package,
            )
        local_filenames[identity.filename] = record
        local_paths[identity.filename] = path
        for target in targets:
            preference = {
                tag: index
                for index, tag in enumerate(
                    ordered_target_tags(target, settings.linux_repair_policy)
                )
            }
            rank = min(
                (preference[tag] for tag in identity.tags if tag in preference),
                default=None,
            )
            if rank is None:
                continue
            key = (identity.package, target)
            published_record = published.get(key)
            if published_record is not None and (
                published_record.wheel.filename == identity.filename
                and published_record.sha256 != record.sha256
            ):
                raise _error(
                    ErrorKind.CONFLICT,
                    f"wheel filename differs from published bytes: {identity.filename}",
                    FailureStage.PLANNING,
                    identity.package,
                    target,
                )
            candidates.setdefault(key, []).append((rank, record, path))
    pending: dict[ArtifactKey, ArtifactRecord] = {}
    for key, options in candidates.items():
        _, record, _ = min(options, key=lambda item: (item[0], item[1].wheel.filename))
        pending[key] = record
    return pending, local_paths


def _create_plan(
    catalog: Catalog,
    settings: ProjectSettings,
    targets: tuple[Target, ...],
    output: Path,
    state_dir: Path,
) -> tuple[tuple[PlanOutcome, ...], dict[PackageVersion, Release], dict[str, Path]]:
    configured_targets = settings.targets()
    validate_catalog_targets(catalog, configured_targets)
    published = _published_inventory(output, settings)
    pending, local_paths = _pending_inventory(
        _wheel_paths(state_dir), catalog, targets, settings, published
    )
    upstream: dict[ArtifactKey, tuple[UpstreamWheel | UpstreamSdist, ...]] = {}
    releases: dict[PackageVersion, Release] = {}
    for entry in catalog.entries:
        for target in targets:
            if entry.platforms is not None and target.platform not in entry.platforms:
                continue
            key = (entry.package, target)
            if target.platform in dict(entry.excluded_platforms):
                continue
            if key in published or key in pending:
                continue
            release = releases.get(entry.package)
            if release is None:
                release = fetch_release(
                    entry.package,
                    allow_yanked_reason=entry.yanked_reason,
                )
                releases[entry.package] = release
            execution_version = target_python_version(target)
            if not release.requires_python.contains(str(execution_version), prereleases=True):
                raise _error(
                    ErrorKind.INCOMPATIBLE,
                    f"Requires-Python {release.requires_python} excludes Python {execution_version}",
                    FailureStage.PLANNING,
                    entry.package,
                    target,
                )
            wheel_files = compatible_wheels(
                release,
                target,
                linux_repair_policy=settings.linux_repair_policy,
            )
            candidates: list[UpstreamWheel | UpstreamSdist] = [
                UpstreamWheel(
                    WheelIdentity.parse(item.filename),
                    item.url,
                    item.sha256,
                )
                for item in wheel_files
            ]
            candidates.extend(
                UpstreamSdist(item.url, item.sha256)
                for item in release.sdists
                if item.requires_python is None
                or item.requires_python.contains(
                    str(execution_version), prereleases=True
                )
            )
            upstream[key] = tuple(candidates)
    outcomes = plan(
        catalog,
        targets,
        published,
        upstream,
        pending,
        configured_targets,
    )
    return outcomes, releases, local_paths


def _outcome_text(outcome: PlanOutcome) -> str:
    if isinstance(outcome, AlreadyPublished):
        return f"published {outcome.package.name}=={outcome.package.version} for {outcome.target.platform}"
    if isinstance(outcome, DownloadWheel):
        return f"download {outcome.wheel.filename} for {outcome.target.platform}"
    if isinstance(outcome, BuildSdist):
        return f"build {outcome.package.name}=={outcome.package.version} for {outcome.target.platform}"
    if isinstance(outcome, ExcludedTarget):
        return f"skip {outcome.package.name}=={outcome.package.version} for {outcome.target.platform}: {outcome.reason}"
    if isinstance(outcome, PendingValidation):
        return f"validate {outcome.wheel.filename} for {outcome.target.platform}"
    if isinstance(outcome, ArtifactConflict):
        return f"conflict {outcome.filename} for {outcome.target.platform}"
    raise AssertionError(type(outcome))


def _build_index(url: str, strict: bool) -> str:
    return validate_build_index(url, strict=strict)


def _command_add(
    arguments: argparse.Namespace, settings: ProjectSettings
) -> int:
    print(_runtime_description(settings))
    resolved = add_packages(
        arguments.catalog,
        arguments.requirements,
        settings.targets(),
        sys.executable,
    )
    print(f"catalog contains {len(resolved)} resolved package pins")
    return 0


def _command_plan(
    arguments: argparse.Namespace,
    catalog: Catalog,
    settings: ProjectSettings,
) -> int:
    targets = _selected_targets(settings, arguments.platform)
    outcomes, _, _ = _create_plan(
        catalog, settings, targets, arguments.output, arguments.state_dir
    )
    if not outcomes:
        print("no package targets are configured")
    else:
        for outcome in outcomes:
            print(_outcome_text(outcome))
    return 0


def _command_build(
    arguments: argparse.Namespace,
    catalog: Catalog,
    settings: ProjectSettings,
) -> int:
    print(_runtime_description(settings))
    targets = _selected_targets(settings, arguments.platform)
    build_index = _build_index(arguments.build_index, arguments.strict_build_deps)
    build_constraints = None
    if arguments.build_constraints is not None:
        try:
            build_constraints = arguments.build_constraints.resolve(strict=True)
            constraints_mode = build_constraints.lstat().st_mode
        except OSError as cause:
            raise _error(
                ErrorKind.CONFIGURATION,
                f"could not read build constraints at {arguments.build_constraints}",
                FailureStage.CONFIGURATION,
            ) from cause
        if stat.S_ISLNK(constraints_mode) or not stat.S_ISREG(constraints_mode):
            raise _error(
                ErrorKind.CONFIGURATION,
                f"build constraints must be a regular file: {arguments.build_constraints}",
                FailureStage.CONFIGURATION,
            )
    prebuild_cwd = Path.cwd().resolve()
    wheel_dir = arguments.state_dir / "wheels"
    _real_directory(arguments.state_dir, create=True)
    _real_directory(wheel_dir, create=True)
    outcomes, releases, _ = _create_plan(
        catalog, settings, targets, arguments.output, arguments.state_dir
    )
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, ArtifactConflict)]
    if conflicts:
        raise _error(
            ErrorKind.CONFLICT,
            _outcome_text(conflicts[0]),
            FailureStage.PLANNING,
            conflicts[0].package,
            conflicts[0].target,
        )
    for outcome in outcomes:
        if isinstance(outcome, BuildSdist):
            require_native_target(outcome.target)
    completed: dict[PackageVersion, list[Path]] = {}
    prepared: dict[str, tuple[Path, str]] = {}
    entries = catalog.by_package()
    for outcome in outcomes:
        if not isinstance(outcome, (DownloadWheel, BuildSdist)):
            continue
        entry = entries[outcome.package]
        release = releases[outcome.package]
        path = None
        for existing in completed.get(outcome.package, ()):
            try:
                validate_wheel_metadata(
                    existing,
                    outcome.package,
                    outcome.target,
                    linux_repair_policy=settings.linux_repair_policy,
                )
                validate_wheel_architectures(existing, outcome.target)
            except CompatibilityRejection:
                continue
            path = existing
            break
        if path is None:
            path = acquire_or_build(
                release,
                outcome.target,
                wheel_dir,
                index_url=build_index,
                linux_repair_policy=settings.linux_repair_policy,
                allow_yanked_reason=entry.yanked_reason,
                likely_binary_ignore=tuple(
                    ignored_path for ignored_path, _ in entry.build.likely_binary_ignore
                ),
                custom_prebuild=entry.build.custom_prebuild,
                build_constraints=build_constraints,
                prebuild_cwd=prebuild_cwd,
                require_build_hashes=arguments.require_build_hashes,
            )
            completed.setdefault(outcome.package, []).append(path)
        if path.name not in prepared:
            origin = (
                "upstream wheel"
                if any(item.filename == path.name for item in release.wheels)
                else "source build"
            )
            prepared[path.name] = (path, origin)
    pending_count = sum(isinstance(item, PendingValidation) for item in outcomes)
    for path, origin in prepared.values():
        print(f"prepared {path.name} from {origin}")
    print(f"prepared {len(prepared)} wheel(s); {pending_count} pending validation")
    return 0


def _validation_policy(entry: PackageEntry) -> ValidationPolicy:
    settings = entry.validation
    extras = tuple(settings.validate_extras)
    if len(extras) > 1:
        raise _error(
            ErrorKind.CONFIGURATION,
            f"{entry.package.name}=={entry.package.version} may configure one validation extra set",
            FailureStage.CONFIGURATION,
            entry.package,
        )
    exceptions = []
    if extras:
        value, reason = extras[0]
        exceptions.append(ValidationException(ExceptionKind.EXTRAS, value, reason))
    exceptions.extend(
        ValidationException(ExceptionKind.MISSING_DEPENDENCY, value, reason)
        for value, reason in settings.validate_incorrect_missing_deps
    )
    exceptions.extend(
        ValidationException(ExceptionKind.SKIP_IMPORT, value, reason)
        for value, reason in settings.validate_skip_imports
    )
    return ValidationPolicy(tuple(exceptions))


@contextmanager
def _validated_artifacts(
    arguments: argparse.Namespace,
    catalog: Catalog,
    settings: ProjectSettings,
) -> Iterator[tuple[tuple[ValidatedArtifact, ...], int]]:
    targets = _selected_targets(settings, arguments.platform)
    validate_catalog_targets(catalog, settings.targets())
    entries = catalog.by_package()
    published = read_published_artifacts(arguments.output)
    published_by_filename = {
        artifact.identity.filename: artifact for artifact in published
    }
    staged: dict[str, tuple[WheelIdentity, Path, str, frozenset[Target]]] = {}
    for path in _wheel_paths(arguments.state_dir):
        identity = WheelIdentity.parse(path.name)
        entry = entries.get(identity.package)
        if entry is None:
            raise _error(
                ErrorKind.INVALID_INPUT,
                f"wheel is not listed at this version in the catalog: "
                f"{identity.package.name}=={identity.package.version}",
                FailureStage.VALIDATION,
                identity.package,
            )
        applicable_targets = frozenset(
            target
            for target in targets
            if (entry.platforms is None or target.platform in entry.platforms)
            and target.platform not in dict(entry.excluded_platforms)
            and identity.tags.intersection(
                ordered_target_tags(target, settings.linux_repair_policy)
            )
        )
        if not applicable_targets:
            continue
        checksum = _sha256(path)
        existing = published_by_filename.get(identity.filename)
        if existing is not None and existing.sha256 != checksum:
            raise _error(
                ErrorKind.CONFLICT,
                f"wheel filename differs from published bytes: {identity.filename}",
                FailureStage.VALIDATION,
                identity.package,
            )
        staged[identity.filename] = (identity, path, checksum, applicable_targets)

    obligations = tuple(
        (entry, target)
        for entry in catalog.entries
        for target in targets
        if (entry.platforms is None or target.platform in entry.platforms)
        and target.platform not in dict(entry.excluded_platforms)
    )
    for entry, target in obligations:
        existing_coverage = any(
            artifact.identity.package == entry.package and target in artifact.targets
            for artifact in published
        )
        staged_candidate = any(
            identity.package == entry.package and target in applicable_targets
            for identity, _, _, applicable_targets in staged.values()
        )
        if not existing_coverage and not staged_candidate:
            raise _error(
                ErrorKind.MISSING_ARTIFACT,
                f"catalog package has no candidate wheel for {target.platform}",
                FailureStage.VALIDATION,
                entry.package,
                target,
            )

    targets_to_validate = {
        filename: frozenset(
            target
            for target in applicable_targets
            if filename not in published_by_filename
            or target not in published_by_filename[filename].targets
        )
        for filename, (_, _, _, applicable_targets) in staged.items()
    }
    targets_to_validate = {
        filename: candidates
        for filename, candidates in targets_to_validate.items()
        if candidates
    }
    local_platform = _local_platform()
    for target in sorted(
        {target for candidates in targets_to_validate.values() for target in candidates},
        key=lambda value: value.platform,
    ):
        if local_platform != target.platform:
            raise _error(
                ErrorKind.INCOMPATIBLE,
                f"validation requires a native {target.platform} runner; "
                f"current runner is {local_platform or 'unsupported'}",
                FailureStage.VALIDATION,
                target=target,
            )

    policies = {
        package: _validation_policy(entry) for package, entry in entries.items()
    }
    validated_targets: dict[str, set[Target]] = {}
    copied_paths: dict[str, Path] = {}
    no_import_results = 0
    with tempfile.TemporaryDirectory(prefix="mypypi-candidate-") as directory:
        snapshot_root = Path(directory)
        for target in targets:
            candidate_sources: dict[str, tuple[Path, str]] = {}
            for artifact in published:
                if target in artifact.targets:
                    candidate_sources[artifact.identity.filename] = (
                        artifact.path,
                        artifact.sha256,
                    )
            for filename, (identity, path, checksum, applicable_targets) in staged.items():
                if target not in applicable_targets:
                    continue
                existing = published_by_filename.get(filename)
                if existing is not None and target in existing.targets:
                    continue
                candidate_sources[filename] = (path, checksum)

            if not candidate_sources:
                continue
            wheel_set = snapshot_root / target.platform
            wheel_set.mkdir()
            target_paths: dict[str, Path] = {}
            for filename, (source, expected_hash) in sorted(candidate_sources.items()):
                wheel_path = wheel_set / filename
                shutil.copyfile(source, wheel_path)
                if _sha256(wheel_path) != expected_hash:
                    raise _error(
                        ErrorKind.INTEGRITY,
                        f"wheel changed while making candidate snapshot: {filename}",
                        FailureStage.VALIDATION,
                    )
                target_paths[filename] = wheel_path
                copied_paths.setdefault(filename, wheel_path)

            pending = []
            for filename, required_targets in targets_to_validate.items():
                if target not in required_targets:
                    continue
                wheel_path = target_paths.get(filename)
                if wheel_path is None:
                    raise _error(
                        ErrorKind.INTEGRITY,
                        f"candidate wheel is absent from target snapshot: {filename}",
                        FailureStage.VALIDATION,
                    )
                inspect_wheel(
                    wheel_path,
                    target,
                    settings.linux_repair_policy,
                )
                pending.append(wheel_path)
            metadata = validate_wheels(
                tuple(pending),
                targets=(target,),
                wheel_set=wheel_set,
                policies=policies,
                linux_repair_policy=settings.linux_repair_policy,
            )
            for item in metadata:
                filename = item.identity.filename
                validated_targets.setdefault(filename, set()).add(target)
                if not item.imports:
                    no_import_results += 1
            for filename, wheel_path in target_paths.items():
                if _sha256(wheel_path) != candidate_sources[filename][1]:
                    raise _error(
                        ErrorKind.INTEGRITY,
                        f"candidate wheel changed during validation: {filename}",
                        FailureStage.VALIDATION,
                    )

        new_artifacts = []
        for filename, (identity, _, checksum, _) in sorted(staged.items()):
            covered = validated_targets.get(filename)
            if not covered:
                continue
            new_artifacts.append(
                ValidatedArtifact(
                    identity,
                    copied_paths[filename],
                    checksum,
                    frozenset(covered),
                )
            )

        coverage = {
            (artifact.identity.package, target)
            for artifact in published
            for target in artifact.targets
        }
        coverage.update(
            (WheelIdentity.parse(filename).package, target)
            for filename, covered in validated_targets.items()
            for target in covered
        )
        missing = next(
            (
                (entry, target)
                for entry, target in obligations
                if (entry.package, target) not in coverage
            ),
            None,
        )
        if missing is not None:
            entry, target = missing
            raise _error(
                ErrorKind.MISSING_ARTIFACT,
                f"catalog package has no validated wheel for {target.platform}",
                FailureStage.VALIDATION,
                entry.package,
                target,
            )
        yield tuple(new_artifacts), no_import_results


def _command_validate(
    arguments: argparse.Namespace,
    catalog: Catalog,
    settings: ProjectSettings,
) -> int:
    print(_runtime_description(settings))
    with _validated_artifacts(arguments, catalog, settings) as (artifacts, no_imports):
        print(
            f"validated {len(artifacts)} new wheel(s) from the local candidate snapshot; "
            f"{no_imports} wheel-target pair(s) had no importable modules"
        )
    return 0


def _command_publish(
    arguments: argparse.Namespace,
    catalog: Catalog,
    settings: ProjectSettings,
) -> int:
    print(_runtime_description(settings))
    with _validated_artifacts(arguments, catalog, settings) as (new_artifacts, no_imports):
        entries = catalog.by_package()
        yanked: dict[str, str | None] = {}
        for artifact in read_published_artifacts(arguments.output):
            entry = entries.get(artifact.identity.package)
            if entry is None:
                continue
            if entry.clear_yanked:
                yanked[artifact.identity.filename] = None
            elif entry.yanked_reason is not None:
                yanked[artifact.identity.filename] = entry.yanked_reason
        for artifact in new_artifacts:
            entry = entries[artifact.identity.package]
            if entry.clear_yanked:
                yanked[artifact.identity.filename] = None
            elif entry.yanked_reason is not None:
                yanked[artifact.identity.filename] = entry.yanked_reason
        current = publish(new_artifacts, arguments.output, yanked=yanked)
    print(
        f"published {len(new_artifacts)} new wheel(s) at {current}; "
        f"{no_imports} wheel-target pair(s) had no importable modules"
    )
    return 0


def _format_error(error: MypypiError, settings: ProjectSettings | None) -> str:
    context = error.context
    details = []
    if context.package is not None:
        details.append(f"{context.package.name}=={context.package.version}")
    if context.target is not None:
        details.append(f"Python {context.target.python_version} on {context.target.platform}")
    elif settings is not None:
        details.append(f"Python {settings.python_version}")
    details.append(f"stage {context.stage}")
    return f"mypypi: {error.detail} ({', '.join(details)})"


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        parser.print_help()
        return 0
    settings: ProjectSettings | None = None
    try:
        settings = load_project_settings(arguments.config)
        _runtime_description(settings)
        catalog = _load_catalog(arguments.catalog)
        if arguments.command == "add":
            return _command_add(arguments, settings)
        if arguments.command == "plan":
            return _command_plan(arguments, catalog, settings)
        if arguments.command == "build":
            return _command_build(arguments, catalog, settings)
        if arguments.command == "validate":
            return _command_validate(arguments, catalog, settings)
        if arguments.command == "publish":
            return _command_publish(arguments, catalog, settings)
        raise AssertionError(arguments.command)
    except MypypiError as error:
        print(_format_error(error, settings), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
