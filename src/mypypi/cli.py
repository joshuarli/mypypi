from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from packaging.version import Version

from .acquire import Release, compatible_wheels, fetch_release
from .add import add_packages
from .build import build_from_sdist, require_native_target, select_or_download
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
from .planner import ArtifactKey, ArtifactRecord, UpstreamSdist, UpstreamWheel, plan
from .platforms import ordered_target_tags
from .store import publish
from .validate import (
    ExceptionKind,
    ValidationException,
    ValidationPolicy,
    validate_wheels,
)

_DEFAULT_BUILD_INDEX = "https://pypi.org/simple"
def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mypypi",
        description="Build and publish validated Python wheels to a local index.",
    )
    parser.add_argument("--catalog", type=Path, default=Path("packages.toml"))
    parser.add_argument("--state-dir", type=Path, default=Path(".mypypi"))
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
    validate.add_argument("--index-url", required=True)
    _add_platform_arguments(validate)

    publish_command = commands.add_parser("publish", help="publish validated wheels to a local index")
    publish_command.add_argument("--output", type=Path, required=True)
    publish_command.add_argument("--index-url", required=True)
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
                key = (identity.package, target)
                record = ArtifactRecord(identity, checksum)
                existing = inventory.get(key)
                if existing is not None and existing != record:
                    raise ValueError(f"multiple published wheels cover {identity.package} on {target.platform}")
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
    inventory = _published_inventory(output, settings)
    grouped: dict[str, tuple[ArtifactRecord, set[Target]]] = {}
    for (_, target), record in inventory.items():
        if record.wheel.filename not in grouped:
            grouped[record.wheel.filename] = (record, set())
        grouped[record.wheel.filename][1].add(target)
    return tuple(
        ValidatedArtifact(
            record.wheel,
            output / "wheels" / filename,
            record.sha256,
            frozenset(targets),
        )
        for filename, (record, targets) in sorted(grouped.items())
    )


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
    published = _published_inventory(output, settings)
    pending, local_paths = _pending_inventory(
        _wheel_paths(state_dir), catalog, targets, settings, published
    )
    upstream: dict[ArtifactKey, tuple[UpstreamWheel | UpstreamSdist, ...]] = {}
    releases: dict[PackageVersion, Release] = {}
    for entry in catalog.entries:
        for target in targets:
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
            if not release.requires_python.contains(str(target.python_version), prereleases=True):
                raise _error(
                    ErrorKind.INCOMPATIBLE,
                    f"Requires-Python {release.requires_python} excludes Python {target.python_version}",
                    FailureStage.PLANNING,
                    entry.package,
                    target,
                )
            wheel_files = compatible_wheels(
                release,
                target,
                linux_repair_policy=settings.linux_repair_policy,
            )
            if wheel_files:
                upstream[key] = tuple(
                    UpstreamWheel(
                        WheelIdentity.parse(item.filename),
                        item.url,
                        item.sha256,
                    )
                    for item in wheel_files
                )
            elif release.sdist is not None:
                upstream[key] = (
                    UpstreamSdist(release.sdist.url, release.sdist.sha256),
                )
            else:
                upstream[key] = ()
    outcomes = plan(catalog, targets, published, upstream, pending)
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
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise _error(
            ErrorKind.CONFIGURATION,
            "build dependency index must be an HTTPS URL without credentials",
            FailureStage.CONFIGURATION,
        )
    public_hosts = {"pypi.org", "pythonhosted.org"}
    host = (parsed.hostname or "").rstrip(".").lower()
    if strict and (
        host in public_hosts
        or any(host.endswith(f".{suffix}") for suffix in public_hosts)
    ):
        raise _error(
            ErrorKind.CONFIGURATION,
            "--strict-build-deps requires a curated build dependency index",
            FailureStage.CONFIGURATION,
        )
    return url


def _command_add(
    arguments: argparse.Namespace, settings: ProjectSettings
) -> int:
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
    targets = _selected_targets(settings, arguments.platform)
    build_index = _build_index(arguments.build_index, arguments.strict_build_deps)
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
    completed: set[str] = set()
    count = 0
    entries = catalog.by_package()
    for outcome in outcomes:
        if isinstance(outcome, DownloadWheel):
            if outcome.wheel.filename in completed:
                continue
            entry = entries[outcome.package]
            release = releases[outcome.package]
            path = None
            for candidate in compatible_wheels(
                release,
                outcome.target,
                linux_repair_policy=settings.linux_repair_policy,
            ):
                if candidate.filename != outcome.wheel.filename:
                    continue
                path = candidate
                break
            if path is None:
                raise _error(
                    ErrorKind.INTEGRITY,
                    "planned wheel is missing from release metadata",
                    FailureStage.ACQUISITION,
                    outcome.package,
                    outcome.target,
                )
            file_by_name = {item.filename: item for item in release.wheels}
            selected = file_by_name[path.filename]
            downloaded = select_or_download(
                release,
                outcome.target,
                wheel_dir,
                linux_repair_policy=settings.linux_repair_policy,
                allow_yanked_reason=entry.yanked_reason,
            )
            if downloaded is None:
                raise _error(
                    ErrorKind.MISSING_ARTIFACT,
                    f"no acceptable upstream wheel for {outcome.package.name}=={outcome.package.version}",
                    FailureStage.ACQUISITION,
                    outcome.package,
                    outcome.target,
                )
            if downloaded.name != selected.filename:
                raise _error(
                    ErrorKind.INTEGRITY,
                    "acquired wheel differs from the planned wheel",
                    FailureStage.ACQUISITION,
                    outcome.package,
                    outcome.target,
                )
            completed.add(downloaded.name)
            count += 1
        elif isinstance(outcome, BuildSdist):
            release = releases[outcome.package]
            if release.sdist is None:
                raise _error(
                    ErrorKind.MISSING_ARTIFACT,
                    "planned source archive is missing from release metadata",
                    FailureStage.ACQUISITION,
                    outcome.package,
                    outcome.target,
                )
            entry = entries[outcome.package]
            try:
                require_native_target(outcome.target)
            except MypypiError as cause:
                raise _error(
                    cause.kind,
                    cause.detail,
                    cause.context.stage,
                    outcome.package,
                    outcome.target,
                ) from cause
            built = build_from_sdist(
                outcome.package,
                outcome.target,
                release.sdist,
                wheel_dir,
                index_url=build_index,
                likely_binary_ignore=tuple(
                    path for path, _ in entry.build.likely_binary_ignore
                ),
                custom_prebuild=entry.build.custom_prebuild,
                linux_repair_policy=settings.linux_repair_policy,
                build_constraints=arguments.build_constraints,
                require_build_hashes=arguments.require_build_hashes,
            )
            completed.add(built.name)
            count += 1
    pending_count = sum(isinstance(item, PendingValidation) for item in outcomes)
    print(f"prepared {count} wheel(s); {pending_count} pending validation")
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


def _validated_artifacts(
    arguments: argparse.Namespace,
    catalog: Catalog,
    settings: ProjectSettings,
) -> tuple[ValidatedArtifact, ...]:
    targets = _selected_targets(settings, arguments.platform)
    paths = _wheel_paths(arguments.state_dir)
    entries = catalog.by_package()
    eligible = []
    for path in paths:
        identity = WheelIdentity.parse(path.name)
        if identity.package not in entries:
            raise _error(
                ErrorKind.INVALID_INPUT,
                f"wheel is not listed in the catalog: {identity.package.name}=={identity.package.version}",
                FailureStage.VALIDATION,
                identity.package,
            )
        if any(
            identity.tags.intersection(
                ordered_target_tags(target, settings.linux_repair_policy)
            )
            for target in targets
        ):
            eligible.append(path)
    if not eligible:
        raise _error(
            ErrorKind.MISSING_ARTIFACT,
            "no pending wheels match the selected platforms",
            FailureStage.VALIDATION,
        )
    policies = {
        package: _validation_policy(entry) for package, entry in entries.items()
    }
    metadata = validate_wheels(
        tuple(eligible),
        targets=targets,
        wheel_set=arguments.state_dir / "wheels",
        index_url=arguments.index_url,
        policies=policies,
        linux_repair_policy=settings.linux_repair_policy,
    )
    by_filename = {path.name: path for path in eligible}
    validated: dict[str, ValidatedArtifact] = {}
    for item in metadata:
        path = by_filename[item.identity.filename]
        supported = frozenset(
            target
            for target in targets
            if item.identity.tags.intersection(
                ordered_target_tags(target, settings.linux_repair_policy)
            )
        )
        validated[item.identity.filename] = ValidatedArtifact(
            item.identity,
            path,
            _sha256(path),
            supported,
        )
    if not validated:
        raise _error(
            ErrorKind.MISSING_ARTIFACT,
            "no wheels passed validation for the selected platforms",
            FailureStage.VALIDATION,
        )
    return tuple(validated[name] for name in sorted(validated))


def _command_validate(
    arguments: argparse.Namespace,
    catalog: Catalog,
    settings: ProjectSettings,
) -> int:
    artifacts = _validated_artifacts(arguments, catalog, settings)
    print(f"validated {len(artifacts)} wheel(s)")
    return 0


def _command_publish(
    arguments: argparse.Namespace,
    catalog: Catalog,
    settings: ProjectSettings,
) -> int:
    new_artifacts = _validated_artifacts(arguments, catalog, settings)
    by_filename = {
        artifact.identity.filename: artifact
        for artifact in _published_artifacts(arguments.output, settings)
    }
    for artifact in new_artifacts:
        existing = by_filename.get(artifact.identity.filename)
        if existing is not None:
            if existing.sha256 != artifact.sha256:
                raise _error(
                    ErrorKind.CONFLICT,
                    f"wheel filename differs from published bytes: {artifact.identity.filename}",
                    FailureStage.PUBLICATION,
                    artifact.identity.package,
                )
            artifact = ValidatedArtifact(
                artifact.identity,
                artifact.path,
                artifact.sha256,
                artifact.targets | existing.targets,
            )
        by_filename[artifact.identity.filename] = artifact
    artifacts = tuple(by_filename[name] for name in sorted(by_filename))
    entries = catalog.by_package()
    yanked = {
        artifact.identity.filename: entries[artifact.identity.package].yanked_reason
        for artifact in artifacts
        if entries[artifact.identity.package].yanked_reason is not None
    }
    current = publish(artifacts, arguments.output, yanked=yanked)
    print(f"published {len(artifacts)} wheel(s) at {current}")
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
        settings = load_project_settings()
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
