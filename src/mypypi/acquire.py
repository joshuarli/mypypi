from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from .models import (
    ErrorKind,
    FailureContext,
    FailureStage,
    MypypiError,
    PackageVersion,
    Target,
)
from .platforms import (
    expected_architectures,
    native_architectures_from_bytes,
    ordered_target_tags,
)


@dataclass(frozen=True, slots=True)
class ReleaseFile:
    filename: str
    url: str
    sha256: str
    packagetype: str
    yanked: bool = False


@dataclass(frozen=True, slots=True)
class Release:
    package: PackageVersion
    requires_python: SpecifierSet
    wheels: tuple[ReleaseFile, ...]
    sdist: ReleaseFile | None


def _error(
    kind: ErrorKind,
    detail: str,
    stage: FailureStage,
    package: PackageVersion | None = None,
    target: Target | None = None,
) -> MypypiError:
    return MypypiError(kind, detail, FailureContext(stage, package, target))


def release_from_metadata(
    package: PackageVersion,
    data: dict[str, Any],
    *,
    allow_yanked_reason: str | None = None,
) -> Release:
    try:
        info = data["info"]
        release_version = Version(info["version"])
        if canonicalize_name(info["name"]) != package.name or release_version != package.version:
            raise ValueError("PyPI release metadata does not match requested package and version")
        requires_python = SpecifierSet(info.get("requires_python") or "")
        files = data["urls"]
        wheels: list[ReleaseFile] = []
        sdists: list[ReleaseFile] = []
        for item in files:
            filename = item["filename"]
            file_url = item["url"]
            if urllib.parse.urlsplit(file_url).scheme != "https":
                raise ValueError(f"artifact URL is not HTTPS: {filename}")
            digest = item["digests"]["sha256"]
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(f"invalid SHA-256 metadata for {filename}")
            yanked = item.get("yanked") is not False and item.get("yanked") is not None
            if yanked and not allow_yanked_reason:
                continue
            artifact = ReleaseFile(filename, file_url, digest, item["packagetype"], yanked)
            if artifact.packagetype == "bdist_wheel":
                wheels.append(artifact)
            elif artifact.packagetype == "sdist":
                sdists.append(artifact)
    except (KeyError, TypeError, ValueError, InvalidVersion, InvalidSpecifier) as cause:
        raise _error(
            ErrorKind.INTEGRITY,
            f"invalid PyPI release metadata: {cause}",
            FailureStage.ACQUISITION,
            package,
        ) from cause
    return Release(package, requires_python, tuple(wheels), sdists[0] if sdists else None)


def fetch_release(
    package: PackageVersion,
    *,
    opener: Any = urllib.request.urlopen,
    allow_yanked_reason: str | None = None,
    sleep=time.sleep,
    monotonic=time.monotonic,
    retry_timeout: float = 60 * 60,
) -> Release:
    request = urllib.request.Request(
        f"https://pypi.org/pypi/{package.name}/{package.version}/json",
        headers={"Accept": "application/json"},
    )
    deadline = monotonic() + retry_timeout
    delay = 1.0
    try:
        while True:
            try:
                with opener(request) as response:
                    data = json.load(response)
                break
            except urllib.error.HTTPError as cause:
                remaining = deadline - monotonic()
                if cause.code != 404 or remaining <= 0:
                    raise
                wait = min(delay, remaining)
                sleep(wait)
                delay = min(delay * 2, 60.0)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as cause:
        raise _error(
            ErrorKind.MISSING_ARTIFACT,
            f"could not read PyPI metadata for {package}",
            FailureStage.ACQUISITION,
            package,
        ) from cause
    return release_from_metadata(package, data, allow_yanked_reason=allow_yanked_reason)


def compatible_wheels(
    release: Release,
    target: Target,
    *,
    linux_repair_policy: str = "manylinux_2_28",
) -> tuple[ReleaseFile, ...]:
    if not release.requires_python.contains(str(target.python_version), prereleases=True):
        return ()
    preference = {tag: index for index, tag in enumerate(ordered_target_tags(target, linux_repair_policy))}
    ranked = []
    for wheel in release.wheels:
        try:
            name, version, _, tags = parse_wheel_filename(wheel.filename)
        except (ValueError, InvalidVersion):
            continue
        if canonicalize_name(str(name)) != release.package.name or version != release.package.version:
            continue
        rank = min((preference[tag] for tag in tags if tag in preference), default=None)
        if rank is not None:
            ranked.append((rank, wheel.filename, wheel))
    return tuple(item[2] for item in sorted(ranked))


def _safe_wheel_members(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    members = tuple(archive.infolist())
    if len({member.filename for member in members}) != len(members):
        raise ValueError("wheel archive contains duplicate paths")
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
            raise ValueError(f"unsafe wheel archive member: {member.filename}")
        mode = member.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ValueError(f"wheel archive contains a non-regular member: {member.filename}")
    return members


def validate_wheel_architectures(
    path: Path,
    target: Target,
    *,
    failure_stage: FailureStage = FailureStage.ACQUISITION,
) -> None:
    package = None
    try:
        name, version, _, _ = parse_wheel_filename(path.name)
        package = PackageVersion(str(name), version)
        expected = expected_architectures(path.name)
        actual_target = target.platform.rsplit("-", 1)[-1]
        if expected and actual_target not in expected:
            raise ValueError(f"wheel tag architectures {sorted(expected)} do not include target {actual_target}")
        with zipfile.ZipFile(path) as archive:
            for member in _safe_wheel_members(archive):
                if member.is_dir():
                    continue
                name = member.filename
                native = name.endswith((".so", ".dylib", ".pyd")) or ".so." in name
                mode = member.external_attr >> 16
                executable = bool(mode & 0o111) or ".data/scripts/" in name
                if executable and not native:
                    with archive.open(member) as stream:
                        if stream.read(2) == b"#!":
                            continue
                    native = True
                if not native:
                    continue
                with archive.open(member) as stream:
                    content = stream.read(4096)
                extracted_arches = set()
                for arch in native_architectures_from_bytes(content):
                    extracted_arches.add(arch)
                if not extracted_arches:
                    raise ValueError(f"could not inspect native artifact {name}")
                expected_for_file = set(expected) or {actual_target}
                if not expected_for_file <= extracted_arches:
                    raise ValueError(
                        f"{name} has architectures {sorted(extracted_arches)}, "
                        f"expected {sorted(expected_for_file)}"
                    )
    except (OSError, zipfile.BadZipFile, ValueError) as cause:
        raise _error(
            ErrorKind.INCOMPATIBLE,
            f"invalid or incompatible wheel {path.name}: {cause}",
            failure_stage,
            package,
            target,
        ) from cause


def download_artifact(artifact: ReleaseFile, destination: Path, *, opener: Any = urllib.request.urlopen) -> Path:
    if urllib.parse.urlsplit(artifact.url).scheme != "https":
        raise _error(ErrorKind.INTEGRITY, "artifact download requires HTTPS", FailureStage.ACQUISITION)
    if (
        not artifact.filename
        or artifact.filename in {".", ".."}
        or Path(artifact.filename).name != artifact.filename
        or "/" in artifact.filename
        or "\\" in artifact.filename
        or "\x00" in artifact.filename
    ):
        raise _error(
            ErrorKind.INTEGRITY,
            f"unsafe artifact filename: {artifact.filename!r}",
            FailureStage.ACQUISITION,
        )
    destination = Path(os.path.abspath(destination))
    try:
        destination.mkdir(parents=True, exist_ok=True)
        destination_mode = destination.lstat().st_mode
    except OSError as cause:
        raise _error(
            ErrorKind.IO,
            f"could not create artifact directory {destination}",
            FailureStage.ACQUISITION,
        ) from cause
    if stat.S_ISLNK(destination_mode) or not stat.S_ISDIR(destination_mode):
        raise _error(
            ErrorKind.INVALID_INPUT,
            f"artifact destination must be a real directory: {destination}",
            FailureStage.ACQUISITION,
        )
    path = destination / artifact.filename
    temporary = destination / f".{artifact.filename}.{secrets.token_hex(12)}.part"
    digest = hashlib.sha256()
    try:
        with opener(artifact.url) as response, temporary.open("xb") as output:
            while chunk := response.read(64 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != artifact.sha256:
            raise _error(
                ErrorKind.INTEGRITY,
                f"SHA-256 mismatch for {artifact.filename}",
                FailureStage.ACQUISITION,
            )
        try:
            os.link(temporary, path)
        except FileExistsError as cause:
            raise _error(
                ErrorKind.CONFLICT,
                f"artifact destination already exists: {path}",
                FailureStage.ACQUISITION,
            ) from cause
        directory_fd = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException as cause:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as cleanup:
            cause.add_note(f"could not remove partial artifact {temporary}: {cleanup}")
        if isinstance(cause, MypypiError):
            raise
        if isinstance(cause, (OSError, urllib.error.URLError)):
            raise _error(
                ErrorKind.MISSING_ARTIFACT,
                f"could not download {artifact.filename}",
                FailureStage.ACQUISITION,
            ) from cause
        raise
    try:
        temporary.unlink(missing_ok=True)
    except OSError as cause:
        raise _error(
            ErrorKind.IO,
            f"could not remove temporary artifact {temporary}",
            FailureStage.ACQUISITION,
        ) from cause
    return path
