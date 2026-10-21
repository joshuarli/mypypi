from __future__ import annotations

import email.parser
import hashlib
import json
import os
import secrets
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import parse_tag
from packaging.utils import canonicalize_name, parse_wheel_filename
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
from .platforms import (
    NativeFormatError,
    expected_architectures,
    native_architectures_from_bytes,
    ordered_target_tags,
    target_python_version,
)
from .wheel_metadata import _matches_dist_info


@dataclass(frozen=True, slots=True)
class ReleaseFile:
    filename: str
    url: str
    sha256: str
    packagetype: str
    yanked: bool = False
    requires_python: SpecifierSet | None = None


@dataclass(frozen=True, slots=True)
class Release:
    package: PackageVersion
    requires_python: SpecifierSet
    wheels: tuple[ReleaseFile, ...]
    sdists: tuple[ReleaseFile, ...]


class CompatibilityCategory(StrEnum):
    WHEEL_TAG = "wheel-tag"
    PYTHON_VERSION = "python-version"
    ARCHITECTURE = "architecture"
    REPAIR_POLICY = "repair-policy"


class CompatibilityRejection(MypypiError):
    def __init__(
        self,
        category: CompatibilityCategory,
        detail: str,
        context: FailureContext,
    ):
        super().__init__(ErrorKind.INCOMPATIBLE, detail, context)
        self.category = category


class _HttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        parsed = urllib.parse.urlsplit(new_url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise urllib.error.URLError("artifact redirect must remain credential-free HTTPS")
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


_HTTPS_OPENER = urllib.request.build_opener(_HttpsRedirectHandler())


def _open_url(url: Any, opener: Any = None):
    if opener is None:
        return _HTTPS_OPENER.open(url, timeout=60)
    return opener(url)


def _error(
    kind: ErrorKind,
    detail: str,
    stage: FailureStage,
    package: PackageVersion | None = None,
    target: Target | None = None,
) -> MypypiError:
    return MypypiError(kind, detail, FailureContext(stage, package, target))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_artifact(
    path: Path,
    artifact: ReleaseFile,
    package: PackageVersion | None = None,
    target: Target | None = None,
) -> Path | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    except OSError as cause:
        raise _error(
            ErrorKind.IO,
            f"could not inspect artifact destination {path}",
            FailureStage.ACQUISITION,
            package,
            target,
        ) from cause
    if not stat.S_ISREG(mode):
        raise _error(
            ErrorKind.CONFLICT,
            f"artifact destination is not a regular file: {path}",
            FailureStage.ACQUISITION,
            package,
            target,
        )
    try:
        actual = _sha256(path)
    except OSError as cause:
        raise _error(
            ErrorKind.IO,
            f"could not read existing artifact {path}",
            FailureStage.ACQUISITION,
            package,
            target,
        ) from cause
    if actual != artifact.sha256:
        raise _error(
            ErrorKind.CONFLICT,
            f"artifact destination contains conflicting bytes: {path}",
            FailureStage.ACQUISITION,
            package,
            target,
        )
    return path


def _promote_wheel(
    staged: Path,
    destination: Path,
    artifact: ReleaseFile,
    package: PackageVersion,
    target: Target,
) -> Path:
    final = destination / artifact.filename
    existing = _existing_artifact(final, artifact, package, target)
    if existing is not None:
        return existing
    try:
        os.link(staged, final)
    except FileExistsError:
        existing = _existing_artifact(final, artifact, package, target)
        if existing is None:
            raise _error(
                ErrorKind.CONFLICT,
                f"artifact destination changed during promotion: {final}",
                FailureStage.ACQUISITION,
                package,
                target,
            )
        return existing
    except OSError as cause:
        raise _error(
            ErrorKind.IO,
            f"could not promote validated wheel {final}",
            FailureStage.ACQUISITION,
            package,
            target,
        ) from cause
    try:
        directory_fd = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as cause:
        raise _error(
            ErrorKind.IO,
            f"could not make wheel promotion durable {final}",
            FailureStage.ACQUISITION,
            package,
            target,
        ) from cause
    return final


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
            parsed_file_url = urllib.parse.urlsplit(file_url)
            if (
                parsed_file_url.scheme != "https"
                or parsed_file_url.username
                or parsed_file_url.password
            ):
                raise ValueError(f"artifact URL is not credential-free HTTPS: {filename}")
            digest = item["digests"]["sha256"]
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(f"invalid SHA-256 metadata for {filename}")
            yanked = item.get("yanked") is not False and item.get("yanked") is not None
            if yanked and not allow_yanked_reason:
                continue
            file_requires_python_text = item.get("requires_python")
            if file_requires_python_text is not None and not isinstance(
                file_requires_python_text, str
            ):
                raise ValueError(f"invalid Requires-Python metadata for {filename}")
            file_requires_python = (
                SpecifierSet(file_requires_python_text)
                if file_requires_python_text
                else None
            )
            artifact = ReleaseFile(
                filename,
                file_url,
                digest,
                item["packagetype"],
                yanked,
                file_requires_python,
            )
            if artifact.packagetype == "bdist_wheel":
                try:
                    name, version, _, _ = parse_wheel_filename(filename)
                except (InvalidVersion, ValueError) as cause:
                    raise ValueError(f"invalid wheel filename in PyPI metadata: {filename}") from cause
                if canonicalize_name(str(name)) != package.name or version != package.version:
                    raise ValueError(f"wheel filename does not match PyPI release identity: {filename}")
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
    return Release(package, requires_python, tuple(wheels), tuple(sdists))


def fetch_release(
    package: PackageVersion,
    *,
    opener: Any = None,
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
                with _open_url(request, opener) as response:
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
    execution_version = target_python_version(target)
    if not release.requires_python.contains(str(execution_version), prereleases=True):
        return ()
    preference = {tag: index for index, tag in enumerate(ordered_target_tags(target, linux_repair_policy))}
    ranked = []
    for wheel in release.wheels:
        if wheel.requires_python is not None and not wheel.requires_python.contains(
            str(execution_version), prereleases=True
        ):
            continue
        try:
            name, version, _, tags = parse_wheel_filename(wheel.filename)
        except (ValueError, InvalidVersion):
            continue
        if canonicalize_name(str(name)) != release.package.name or version != release.package.version:
            continue
        rank = min((preference[tag] for tag in tags if tag in preference), default=None)
        if rank is not None:
            ranked.append((rank, wheel.filename, wheel))
    return tuple(item[2] for item in sorted(ranked, key=lambda item: (item[0], item[1])))


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
            raise CompatibilityRejection(
                CompatibilityCategory.WHEEL_TAG,
                f"wheel tag architectures {sorted(expected)} do not include target {actual_target}",
                FailureContext(failure_stage, package, target),
            )
        with zipfile.ZipFile(path) as archive:
            for member in _safe_wheel_members(archive):
                if member.is_dir():
                    continue
                name = member.filename
                native = name.endswith((".so", ".dylib", ".pyd", ".dll", ".exe")) or ".so." in name
                with archive.open(member) as stream:
                    content = stream.read(1024 * 1024)
                if not native and content.startswith(b"#!"):
                    continue
                try:
                    extracted_arches = set(native_architectures_from_bytes(content))
                except NativeFormatError as cause:
                    raise _error(
                        ErrorKind.INTEGRITY,
                        f"malformed native artifact {name}: {cause}",
                        failure_stage,
                        package,
                        target,
                    ) from cause
                native = native or bool(extracted_arches)
                if not extracted_arches:
                    if not native:
                        continue
                    raise ValueError(f"could not inspect native artifact {name}")
                expected_for_file = set(expected) or {actual_target}
                if not expected_for_file <= extracted_arches:
                    raise CompatibilityRejection(
                        CompatibilityCategory.ARCHITECTURE,
                        f"{name} has architectures {sorted(extracted_arches)}, "
                        f"expected {sorted(expected_for_file)}",
                        FailureContext(failure_stage, package, target),
                    )
    except CompatibilityRejection:
        raise
    except (OSError, zipfile.BadZipFile, ValueError) as cause:
        raise _error(
            ErrorKind.INTEGRITY,
            f"invalid or incompatible wheel {path.name}: {cause}",
            failure_stage,
            package,
            target,
        ) from cause


def validate_wheel_metadata(
    path: Path,
    package: PackageVersion,
    target: Target,
    *,
    linux_repair_policy: str = "manylinux_2_28",
    enforce_target_tags: bool = True,
    failure_stage: FailureStage = FailureStage.ACQUISITION,
) -> WheelIdentity:
    context = FailureContext(failure_stage, package, target)
    try:
        identity = WheelIdentity.parse(path.name)
    except ValueError as cause:
        raise _error(
            ErrorKind.INTEGRITY,
            f"invalid wheel filename {path.name}",
            failure_stage,
            package,
            target,
        ) from cause
    if identity.package != package:
        raise _error(
            ErrorKind.INTEGRITY,
            f"wheel filename {path.name} does not match {package.name}=={package.version}",
            failure_stage,
            package,
            target,
        )
    try:
        with zipfile.ZipFile(path) as archive:
            members = _safe_wheel_members(archive)
            names = {member.filename for member in members}
            dist_infos = {
                name.split("/", 1)[0]
                for name in names
                if name.split("/", 1)[0].endswith(".dist-info")
            }
            if len(dist_infos) != 1:
                raise ValueError("wheel must contain exactly one .dist-info directory")
            dist_info = next(iter(dist_infos))
            if not _matches_dist_info(identity, dist_info):
                raise ValueError("wheel .dist-info directory does not match its filename")
            metadata_path = f"{dist_info}/METADATA"
            wheel_path = f"{dist_info}/WHEEL"
            if not {metadata_path, wheel_path, f"{dist_info}/RECORD"} <= names:
                raise ValueError("wheel requires METADATA, WHEEL, and RECORD")
            metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_path))
            wheel_metadata = email.parser.BytesParser().parsebytes(archive.read(wheel_path))
            metadata_package = PackageVersion.parse(metadata["Name"], metadata["Version"])
            requires_python = SpecifierSet(metadata.get("Requires-Python", ""))
            wheel_tags = frozenset().union(*(parse_tag(value) for value in wheel_metadata.get_all("Tag", [])))
            if metadata_package != package:
                raise ValueError("wheel filename and METADATA identity differ")
            if not wheel_tags or wheel_tags != identity.tags:
                raise ValueError("wheel WHEEL tags do not match its filename")
    except (OSError, zipfile.BadZipFile, InvalidSpecifier, TypeError, ValueError) as cause:
        raise _error(
            ErrorKind.INTEGRITY,
            f"invalid wheel metadata in {path.name}: {cause}",
            failure_stage,
            package,
            target,
        ) from cause
    execution_version = target_python_version(target)
    if not requires_python.contains(str(execution_version), prereleases=True):
        raise CompatibilityRejection(
            CompatibilityCategory.PYTHON_VERSION,
            f"wheel Requires-Python excludes CPython {execution_version}",
            context,
        )
    if enforce_target_tags:
        preference = set(ordered_target_tags(target, linux_repair_policy))
        if not preference.intersection(identity.tags):
            raise CompatibilityRejection(
                CompatibilityCategory.WHEEL_TAG,
                f"wheel tags are incompatible with target {target.platform}",
                context,
            )
    return identity


def select_or_download(
    release: Release,
    target: Target,
    destination: Path,
    *,
    linux_repair_policy: str = "manylinux_2_28",
    allow_yanked_reason: str | None = None,
    opener: Any = None,
) -> Path | None:
    execution_version = target_python_version(target)
    if not release.requires_python.contains(str(execution_version), prereleases=True):
        raise CompatibilityRejection(
            CompatibilityCategory.PYTHON_VERSION,
            f"release Requires-Python excludes CPython {execution_version}",
            FailureContext(FailureStage.ACQUISITION, release.package, target),
        )
    candidates = compatible_wheels(release, target, linux_repair_policy=linux_repair_policy)
    by_name: dict[str, ReleaseFile] = {}
    for artifact in release.wheels:
        previous = by_name.get(artifact.filename)
        if previous is not None and previous.sha256 != artifact.sha256:
            raise _error(
                ErrorKind.CONFLICT,
                f"release metadata assigns conflicting bytes to {artifact.filename}",
                FailureStage.ACQUISITION,
                release.package,
                target,
            )
        by_name[artifact.filename] = artifact
    destination = Path(os.path.abspath(destination))
    try:
        destination.mkdir(parents=True, exist_ok=True)
        mode = destination.lstat().st_mode
    except OSError as cause:
        raise _error(
            ErrorKind.IO,
            f"could not create artifact directory {destination}",
            FailureStage.ACQUISITION,
            release.package,
            target,
        ) from cause
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise _error(
            ErrorKind.INVALID_INPUT,
            f"artifact destination must be a real directory: {destination}",
            FailureStage.ACQUISITION,
            release.package,
            target,
        )
    last_rejection = None
    seen = set()
    for candidate in candidates:
        if candidate.filename in seen:
            continue
        seen.add(candidate.filename)
        artifact = by_name[candidate.filename]
        if artifact.yanked and not allow_yanked_reason:
            continue
        existing = _existing_artifact(
            destination / artifact.filename,
            artifact,
            release.package,
            target,
        )
        if existing is not None:
            try:
                validate_wheel_metadata(
                    existing,
                    release.package,
                    target,
                    linux_repair_policy=linux_repair_policy,
                )
                validate_wheel_architectures(existing, target)
            except CompatibilityRejection as cause:
                last_rejection = cause
                continue
            return existing
        with tempfile.TemporaryDirectory(prefix=".mypypi-acquire-", dir=destination) as directory:
            stage = Path(directory)
            try:
                downloaded = download_artifact(artifact, stage, opener=opener)
                validate_wheel_metadata(
                    downloaded,
                    release.package,
                    target,
                    linux_repair_policy=linux_repair_policy,
                )
                validate_wheel_architectures(downloaded, target)
                return _promote_wheel(downloaded, destination, artifact, release.package, target)
            except CompatibilityRejection as cause:
                last_rejection = cause
                continue
            except MypypiError as cause:
                if cause.context.package is None or cause.context.target is None:
                    raise MypypiError(
                        cause.kind,
                        cause.detail,
                        FailureContext(cause.context.stage, release.package, target),
                    ) from cause
                raise
    if last_rejection is not None:
        raise last_rejection
    if release.wheels and candidates == ():
        raise CompatibilityRejection(
            CompatibilityCategory.WHEEL_TAG,
            f"no release wheel is compatible with target {target.platform}",
            FailureContext(FailureStage.ACQUISITION, release.package, target),
        )
    return None


def download_artifact(artifact: ReleaseFile, destination: Path, *, opener: Any = None) -> Path:
    parsed_url = urllib.parse.urlsplit(artifact.url)
    if parsed_url.scheme != "https" or parsed_url.username or parsed_url.password:
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
    existing = _existing_artifact(path, artifact)
    if existing is not None:
        return existing
    temporary = destination / f".{artifact.filename}.{secrets.token_hex(12)}.part"
    digest = hashlib.sha256()
    try:
        with _open_url(artifact.url, opener) as response, temporary.open("xb") as output:
            response_url = getattr(response, "geturl", lambda: artifact.url)()
            redirected = urllib.parse.urlsplit(response_url)
            if redirected.scheme != "https" or redirected.username or redirected.password:
                raise _error(
                    ErrorKind.INTEGRITY,
                    "artifact redirect must remain credential-free HTTPS",
                    FailureStage.ACQUISITION,
                )
            while chunk := response.read(64 * 1024):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != artifact.sha256:
            raise _error(
                ErrorKind.INTEGRITY,
                f"SHA-256 mismatch for {artifact.filename}",
                FailureStage.ACQUISITION,
            )
        try:
            os.link(temporary, path)
        except FileExistsError as cause:
            existing = _existing_artifact(path, artifact)
            if existing is None:
                raise _error(
                    ErrorKind.CONFLICT,
                    f"artifact destination changed during download: {path}",
                    FailureStage.ACQUISITION,
                ) from cause
            temporary.unlink(missing_ok=True)
            return existing
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
