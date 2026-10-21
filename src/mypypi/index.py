from __future__ import annotations

import hashlib
import html
import re
import stat
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import PurePosixPath
from urllib.parse import quote

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from .models import ErrorKind, FailureContext, FailureStage, MypypiError, Target, ValidatedArtifact
from .platforms import current_platform, target_python_version


@dataclass(frozen=True, slots=True)
class IndexArtifact:
    artifact: ValidatedArtifact
    requires_python: str | None
    metadata: bytes
    metadata_sha256: str
    yanked_reason: str | None = None


def _publication_error(detail: str, artifact: ValidatedArtifact | None = None) -> MypypiError:
    return MypypiError(
        ErrorKind.INTEGRITY if artifact else ErrorKind.INVALID_INPUT,
        detail,
        FailureContext(
            FailureStage.PUBLICATION,
            artifact.identity.package if artifact else None,
            artifact.targets and sorted(artifact.targets, key=lambda target: target.platform)[0] if artifact else None,
        ),
    )


def _wheel_metadata(artifact: ValidatedArtifact) -> tuple[str | None, bytes]:
    try:
        with zipfile.ZipFile(artifact.path) as wheel:
            members = wheel.infolist()
            if len({member.filename for member in members}) != len(members):
                raise _publication_error("wheel contains duplicate archive paths", artifact)
            for member in members:
                path = PurePosixPath(member.filename)
                parts = member.filename.rstrip("/").split("/")
                if (
                    not member.filename
                    or path.is_absolute()
                    or not parts
                    or any(part in {"", ".", ".."} for part in parts)
                    or ":" in parts[0]
                    or "\\" in member.filename
                    or "\x00" in member.filename
                ):
                    raise _publication_error("wheel contains an archive path that escapes its root", artifact)
                mode = member.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise _publication_error("wheel contains a non-regular archive member", artifact)
            metadata_members = [
                member.filename
                for member in members
                if member.filename.endswith(".dist-info/METADATA")
            ]
            if len(metadata_members) != 1:
                raise _publication_error("wheel must contain exactly one dist-info/METADATA", artifact)
            metadata = wheel.read(metadata_members[0])
    except MypypiError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as cause:
        raise _publication_error("could not read wheel metadata", artifact) from cause

    try:
        parsed_metadata = BytesParser().parsebytes(metadata)
        names = parsed_metadata.get_all("Name", [])
        versions = parsed_metadata.get_all("Version", [])
        requires_python_values = parsed_metadata.get_all("Requires-Python", [])
        requires_python = requires_python_values[0] if requires_python_values else None
    except (UnicodeError, ValueError) as cause:
        raise _publication_error("wheel metadata is invalid", artifact) from cause
    if len(names) != 1 or canonicalize_name(names[0]) != artifact.identity.package.name:
        raise _publication_error("wheel metadata name does not match its filename", artifact)
    try:
        version_matches = len(versions) == 1 and Version(versions[0]) == artifact.identity.package.version
    except ValueError as cause:
        raise _publication_error("wheel metadata contains an invalid version", artifact) from cause
    if not version_matches:
        raise _publication_error("wheel metadata version does not match its filename", artifact)
    if len(requires_python_values) > 1:
        raise _publication_error("wheel metadata has duplicate Requires-Python fields", artifact)
    if requires_python is not None:
        try:
            specifier = SpecifierSet(requires_python)
        except InvalidSpecifier as cause:
            raise _publication_error("wheel metadata has invalid Requires-Python", artifact) from cause
        excluded_versions = sorted(
            {
                str(target.python_version)
                for target in artifact.targets
                if not _supports_target_python(specifier, target)
            }
        )
        if excluded_versions:
            raise _publication_error(
                "wheel Requires-Python metadata excludes CPython "
                + ", ".join(excluded_versions),
                artifact,
            )
    return requires_python, metadata


def _supports_target_python(specifier: SpecifierSet, target: Target) -> bool:
    if current_platform() == target.platform:
        return specifier.contains(
            str(target_python_version(target)), prereleases=True
        )
    major, minor = target.python_version.release[:2]
    candidates = {Version(f"{major}.{minor}.0"), Version(f"{major}.{minor}.1")}
    for requirement in specifier:
        try:
            boundary = Version(requirement.version.removesuffix(".*"))
        except ValueError:
            continue
        if boundary.release[:2] != (major, minor):
            continue
        patch = boundary.release[2] if len(boundary.release) > 2 else 0
        candidates.update(
            Version(f"{major}.{minor}.{value}")
            for value in (patch - 1, patch, patch + 1)
            if value >= 0
        )
    return any(specifier.contains(str(candidate), prereleases=True) for candidate in candidates)


def prepare_index_artifacts(
    artifacts: tuple[ValidatedArtifact, ...],
    yanked: dict[str, str | None] | None = None,
) -> tuple[IndexArtifact, ...]:
    if any(type(artifact) is not ValidatedArtifact for artifact in artifacts):
        raise _publication_error("publication accepts only ValidatedArtifact values")
    try:
        reasons = {} if yanked is None else dict(yanked)
    except (TypeError, ValueError) as cause:
        raise _publication_error("yanked metadata must map wheel filenames to reasons") from cause
    for filename, reason in reasons.items():
        if not isinstance(filename, str) or not filename:
            raise _publication_error("yanked metadata filenames must be non-empty strings")
        if not isinstance(reason, str) or not reason.strip():
            raise _publication_error("yanked reason must be a non-empty string")
    unknown = reasons.keys() - {artifact.identity.filename for artifact in artifacts}
    if unknown:
        raise _publication_error(f"yanked metadata names unknown wheels: {', '.join(sorted(unknown))}")
    prepared: list[IndexArtifact] = []
    seen: set[str] = set()
    for artifact in artifacts:
        if artifact.identity.filename in seen:
            raise _publication_error(f"duplicate wheel filename: {artifact.identity.filename}", artifact)
        seen.add(artifact.identity.filename)
        reason = reasons.get(artifact.identity.filename)
        try:
            digest = hashlib.sha256()
            with artifact.path.open("rb") as wheel:
                while chunk := wheel.read(64 * 1024):
                    digest.update(chunk)
            actual_sha256 = digest.hexdigest()
        except OSError as cause:
            raise _publication_error("could not read validated wheel", artifact) from cause
        if actual_sha256 != artifact.sha256:
            raise _publication_error("wheel bytes changed after validation", artifact)
        requires_python, metadata = _wheel_metadata(artifact)
        prepared.append(
            IndexArtifact(
                artifact,
                requires_python,
                metadata,
                hashlib.sha256(metadata).hexdigest(),
                reason,
            )
        )
    return tuple(
        sorted(
            prepared,
            key=lambda item: (
                item.artifact.identity.package.name,
                item.artifact.identity.filename,
            ),
        )
    )


def render_root_page(artifacts: tuple[IndexArtifact, ...]) -> bytes:
    lines = ["<!DOCTYPE html>", "<html>", "  <body>"]
    for name in sorted({project_name(item) for item in artifacts}):
        href = quote(f"{name}/", safe="/-._~")
        lines.append(f'    <a href="{html.escape(href, quote=True)}">{html.escape(name)}</a>')
    lines.extend(["  </body>", "</html>", ""])
    return "\n".join(lines).encode("utf-8")


def render_project_page(artifacts: tuple[IndexArtifact, ...]) -> bytes:
    lines = ["<!DOCTYPE html>", "<html>", "  <body>"]
    for item in sorted(artifacts, key=lambda value: value.artifact.identity.filename):
        filename = item.artifact.identity.filename
        href = quote(f"../../wheels/{filename}", safe="/-._~")
        href += f"#sha256={item.artifact.sha256}"
        attributes = [f'href="{html.escape(href, quote=True)}"']
        if item.requires_python is not None:
            attributes.append(
                f'data-requires-python="{html.escape(item.requires_python, quote=True)}"'
            )
        if item.yanked_reason is not None:
            attributes.append(f'data-yanked="{html.escape(item.yanked_reason, quote=True)}"')
        attributes.append(
            f'data-dist-info-metadata="sha256={item.metadata_sha256}"'
        )
        attributes.append(
            f'data-core-metadata="sha256={item.metadata_sha256}"'
        )
        lines.append(f"    <a {' '.join(attributes)}>{html.escape(filename)}</a>")
    lines.extend(["  </body>", "</html>", ""])
    return "\n".join(lines).encode("utf-8")


def project_name(artifact: IndexArtifact) -> str:
    return re.sub(r"[-_.]+", "-", canonicalize_name(artifact.artifact.identity.package.name))
