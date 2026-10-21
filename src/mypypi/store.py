from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shutil
import stat
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path

from packaging.version import Version

from .index import (
    IndexArtifact,
    prepare_index_artifacts,
    project_name,
    render_project_page,
    render_root_page,
)
from .models import ErrorKind, FailureContext, FailureStage, MypypiError, Target, ValidatedArtifact, WheelIdentity

_STAGING_PREFIX = ".mypypi-staging-"
_STAGING_OWNER = ".mypypi-owner"


def _error(
    kind: ErrorKind,
    detail: str,
    path: Path | None = None,
    item: IndexArtifact | None = None,
) -> MypypiError:
    suffix = f": {path}" if path is not None else ""
    target = (
        sorted(item.artifact.targets, key=lambda value: value.platform)[0]
        if item is not None
        else None
    )
    return MypypiError(
        kind,
        f"{detail}{suffix}",
        FailureContext(
            FailureStage.PUBLICATION,
            item.artifact.identity.package if item is not None else None,
            target,
        ),
    )


def _remove_temporary(path: Path, failure: BaseException | None) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as cause:
        if failure is None:
            raise _error(ErrorKind.IO, "could not remove temporary publication file", path) from cause
        failure.add_note(f"could not remove temporary publication file {path}: {cause}")


def _ensure_output_root(output: Path) -> Path:
    output = Path(os.path.abspath(output))
    try:
        output.lstat()
    except FileNotFoundError:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as cause:
        raise _error(ErrorKind.IO, "could not inspect output root", output) from cause
    try:
        mode = output.lstat().st_mode
    except OSError as cause:
        raise _error(ErrorKind.IO, "could not inspect output root", output) from cause
    if stat.S_ISLNK(mode):
        raise _error(ErrorKind.INVALID_INPUT, "output root must not be a symlink", output)
    if not stat.S_ISDIR(mode):
        raise _error(ErrorKind.INVALID_INPUT, "output root must be a directory", output)
    return output


class _ProcessLock:
    def __init__(self, path: Path, *, blocking: bool = False):
        self.path = path
        self.blocking = blocking
        self.fd: int | None = None

    def __enter__(self) -> _ProcessLock:
        try:
            self.fd = os.open(
                self.path,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
            if not stat.S_ISREG(os.fstat(self.fd).st_mode):
                raise OSError("publication lock is not a regular file")
            operation = fcntl.LOCK_EX
            if not self.blocking:
                operation |= fcntl.LOCK_NB
            fcntl.flock(self.fd, operation)
        except BlockingIOError as cause:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            raise _error(ErrorKind.CONFLICT, "another publisher holds the output lock", self.path) from cause
        except BaseException as cause:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if not isinstance(cause, OSError):
                raise
            raise _error(ErrorKind.IO, "could not acquire output lock", self.path) from cause
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        if self.fd is None:
            return
        descriptor = self.fd
        self.fd = None
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
                exc.add_note(f"could not release publication lock {self.path}: {cause}")
        elif cleanup_errors:
            raise _error(ErrorKind.IO, "could not release publication lock", self.path) from cleanup_errors[0]


def _recover_staging(output: Path) -> None:
    for candidate in output.iterdir():
        if not candidate.name.startswith(_STAGING_PREFIX):
            continue
        try:
            if not stat.S_ISDIR(candidate.lstat().st_mode):
                continue
        except (FileNotFoundError, UnicodeError):
            continue
        marker = candidate / _STAGING_OWNER
        try:
            marker_mode = marker.lstat().st_mode
            if stat.S_ISREG(marker_mode) and marker.read_text(encoding="ascii") == "mypypi\n":
                shutil.rmtree(candidate)
        except (FileNotFoundError, UnicodeError):
            continue
        except OSError as cause:
            raise _error(ErrorKind.IO, "could not recover abandoned staging directory", candidate) from cause


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(exist_ok=True)
        mode = path.lstat().st_mode
    except OSError as cause:
        raise _error(ErrorKind.IO, "could not create publication directory", path) from cause
    if not stat.S_ISDIR(mode):
        raise _error(ErrorKind.INVALID_INPUT, "publication path must be a real directory", path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_wheel_sha256(path: Path, item: IndexArtifact) -> str:
    try:
        mode = path.lstat().st_mode
    except OSError as cause:
        raise _error(ErrorKind.IO, "could not inspect stored wheel", path, item) from cause
    if not stat.S_ISREG(mode):
        raise _error(ErrorKind.INTEGRITY, "stored wheel path is not a regular file", path, item)
    try:
        return _file_sha256(path)
    except OSError as cause:
        raise _error(ErrorKind.IO, "could not read stored wheel", path, item) from cause


def _store_wheel(output: Path, item: IndexArtifact) -> None:
    wheels = output / "wheels"
    _ensure_directory(wheels)
    destination = wheels / item.artifact.identity.filename
    try:
        info = destination.lstat()
    except FileNotFoundError:
        info = None
    except OSError as cause:
        raise _error(ErrorKind.IO, "could not inspect stored wheel", destination) from cause
    if info is not None:
        if not stat.S_ISREG(info.st_mode):
            raise _error(ErrorKind.INTEGRITY, "stored wheel path is not a regular file", destination, item)
        if _existing_wheel_sha256(destination, item) != item.artifact.sha256:
            raise _error(ErrorKind.CONFLICT, "wheel filename already exists with different bytes", destination, item)
        _store_metadata(wheels, item)
        return
    temporary = wheels / f".{item.artifact.identity.filename}.{secrets.token_hex(8)}.tmp"
    failure: BaseException | None = None
    temporary_created = False
    try:
        digest = hashlib.sha256()
        with temporary.open("xb") as target:
            temporary_created = True
            with item.artifact.path.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        if digest.hexdigest() != item.artifact.sha256:
            raise _error(ErrorKind.INTEGRITY, "validated wheel changed while it was stored", destination, item)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if _existing_wheel_sha256(destination, item) != item.artifact.sha256:
                raise _error(
                    ErrorKind.CONFLICT,
                    "wheel filename already exists with different bytes",
                    destination,
                    item,
                )
        _store_metadata(wheels, item)
        directory_fd = os.open(wheels, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except MypypiError as cause:
        failure = cause
        raise
    except OSError as cause:
        failure = _error(ErrorKind.IO, "could not store wheel", destination)
        raise failure from cause
    finally:
        if temporary_created:
            _remove_temporary(temporary, failure)


def _store_metadata(wheels: Path, item: IndexArtifact) -> None:
    destination = wheels / f"{item.artifact.identity.filename}.metadata"
    try:
        info = destination.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise _error(ErrorKind.INTEGRITY, "stored metadata path is not a regular file", destination, item)
        existing = destination.read_bytes()
    except FileNotFoundError:
        existing = None
    except MypypiError:
        raise
    except OSError as cause:
        raise _error(ErrorKind.IO, "could not read stored metadata", destination) from cause
    if existing is not None:
        if existing != item.metadata:
            raise _error(
                ErrorKind.CONFLICT,
                "wheel metadata sidecar already exists with different bytes",
                destination,
                item,
            )
        return
    temporary = wheels / f".{item.artifact.identity.filename}.{secrets.token_hex(8)}.metadata.tmp"
    failure: BaseException | None = None
    temporary_created = False
    try:
        with temporary.open("xb") as target:
            temporary_created = True
            target.write(item.metadata)
            target.flush()
            os.fsync(target.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            try:
                mode = destination.lstat().st_mode
            except OSError as cause:
                raise _error(ErrorKind.IO, "could not inspect stored metadata", destination, item) from cause
            if not stat.S_ISREG(mode) or destination.read_bytes() != item.metadata:
                raise _error(
                    ErrorKind.CONFLICT,
                    "wheel metadata sidecar already exists with different bytes",
                    destination,
                    item,
                )
        directory_fd = os.open(wheels, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except MypypiError as cause:
        failure = cause
        raise
    except OSError as cause:
        failure = _error(ErrorKind.IO, "could not store wheel metadata", destination)
        raise failure from cause
    finally:
        if temporary_created:
            _remove_temporary(temporary, failure)


def _manifest(items: tuple[IndexArtifact, ...]) -> bytes:
    records = [
        {
            "filename": item.artifact.identity.filename,
            "name": item.artifact.identity.package.name,
            "version": str(item.artifact.identity.package.version),
            "sha256": item.artifact.sha256,
            "metadata_sha256": item.metadata_sha256,
            "requires_python": item.requires_python,
            "yanked": item.yanked_reason,
            "targets": sorted(
                f"{target.python_version}:{target.platform}"
                for target in item.artifact.targets
            ),
        }
        for item in items
    ]
    return (
        json.dumps(
            {"schema_version": 1, "artifacts": records},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _project_pages(items: tuple[IndexArtifact, ...]) -> dict[str, bytes]:
    grouped: dict[str, list[IndexArtifact]] = {}
    for item in items:
        grouped.setdefault(project_name(item), []).append(item)
    return {
        "simple/index.html": render_root_page(items),
        **{
            f"simple/{name}/index.html": render_project_page(tuple(grouped[name]))
        for name in sorted(grouped)
        },
    }


def _verify_generation(
    generation: Path,
    manifest: bytes,
    pages: Mapping[str, bytes],
) -> None:
    manifest_path = generation / "manifest.json"
    try:
        wheels_mode = (generation / "wheels").lstat().st_mode
        if not stat.S_ISLNK(wheels_mode) or os.readlink(generation / "wheels") != "../../wheels":
            raise _error(ErrorKind.INTEGRITY, "existing package index wheel URL path is invalid", generation / "wheels")
        simple_mode = (generation / "simple").lstat().st_mode
        if stat.S_ISLNK(simple_mode) or not stat.S_ISDIR(simple_mode):
            raise _error(ErrorKind.INTEGRITY, "existing package index simple path is invalid", generation)
        manifest_mode = manifest_path.lstat().st_mode
        if not stat.S_ISREG(manifest_mode) or manifest_path.read_bytes() != manifest:
            raise _error(ErrorKind.INTEGRITY, "existing package index generation has an invalid manifest", generation)
        for relative_path, expected in pages.items():
            page = generation / relative_path
            parent_mode = page.parent.lstat().st_mode
            if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
                raise _error(ErrorKind.INTEGRITY, "existing package index project path is invalid", page.parent)
            page_mode = page.lstat().st_mode
            if not stat.S_ISREG(page_mode) or page.read_bytes() != expected:
                raise _error(ErrorKind.INTEGRITY, "existing package index generation has an invalid project page", page)
    except MypypiError:
        raise
    except OSError as cause:
        raise _error(ErrorKind.INTEGRITY, "existing package index generation is incomplete", generation) from cause


def _remove_staging_owner(generation: Path) -> None:
    marker = generation / _STAGING_OWNER
    try:
        mode = marker.lstat().st_mode
        if not stat.S_ISREG(mode) or marker.read_text(encoding="ascii") != "mypypi\n":
            raise _error(ErrorKind.INTEGRITY, "package index generation has an invalid staging marker", marker)
        marker.unlink()
        directory_fd = os.open(generation, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileNotFoundError:
        return
    except MypypiError:
        raise
    except (OSError, UnicodeError) as cause:
        raise _error(ErrorKind.IO, "could not remove package index staging marker", marker) from cause


def _write_generation(output: Path, items: tuple[IndexArtifact, ...], manifest: bytes) -> str:
    pages = _project_pages(items)
    digest = hashlib.sha256(b"mypypi-simple-html-v2\0")
    digest.update(manifest)
    for page_name, content in sorted(pages.items()):
        digest.update(page_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
    for item in items:
        digest.update(item.metadata)
    generation = digest.hexdigest()
    generations = output / "generations"
    _ensure_directory(generations)
    final = generations / generation
    try:
        final_mode = final.lstat().st_mode
    except FileNotFoundError:
        final_mode = None
    except OSError as cause:
        raise _error(ErrorKind.IO, "could not inspect package index generation", final) from cause
    if final_mode is not None:
        if not stat.S_ISDIR(final_mode):
            raise _error(ErrorKind.INTEGRITY, "package index generation is not a real directory", final)
        _verify_generation(final, manifest, pages)
        _remove_staging_owner(final)
        return generation
    staging = output / f"{_STAGING_PREFIX}{secrets.token_hex(16)}"
    failure: BaseException | None = None
    staging_created = False
    try:
        staging.mkdir()
        staging_created = True
        (staging / _STAGING_OWNER).write_text("mypypi\n", encoding="ascii")
        simple = staging / "simple"
        simple.mkdir()
        (staging / "wheels").symlink_to(Path("..") / ".." / "wheels")
        for relative_path, content in pages.items():
            page = staging / relative_path
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_bytes(content)
        (staging / "manifest.json").write_bytes(manifest)
        for directory, _, files in os.walk(staging, topdown=False, followlinks=False):
            directory_path = Path(directory)
            for filename in files:
                file_path = directory_path / filename
                if stat.S_ISLNK(file_path.lstat().st_mode):
                    continue
                file_fd = os.open(file_path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(file_fd)
                finally:
                    os.close(file_fd)
            directory_fd = os.open(directory_path, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        os.replace(staging, final)
        _remove_staging_owner(final)
        generations_fd = os.open(generations, os.O_RDONLY)
        try:
            os.fsync(generations_fd)
        finally:
            os.close(generations_fd)
        return generation
    except OSError as cause:
        failure = _error(ErrorKind.IO, "could not stage package index generation", staging)
        raise failure from cause
    except BaseException as cause:
        failure = cause
        raise
    finally:
        try:
            try:
                staging_mode = staging.lstat().st_mode
            except FileNotFoundError:
                staging_mode = None
            if staging_created and staging_mode is not None:
                if stat.S_ISDIR(staging_mode):
                    shutil.rmtree(staging)
                else:
                    staging.unlink()
        except OSError as cause:
            if failure is None:
                raise _error(ErrorKind.IO, "could not remove package index staging directory", staging) from cause
            failure.add_note(f"could not remove package index staging directory {staging}: {cause}")


def _promote(output: Path, generation: str) -> None:
    current = output / "current"
    if current.is_symlink() and os.readlink(current) == f"generations/{generation}":
        return
    try:
        current_mode = current.lstat().st_mode
    except FileNotFoundError:
        current_mode = None
    except OSError as cause:
        raise _error(ErrorKind.IO, "could not inspect current index path", current) from cause
    if current_mode is not None and not stat.S_ISLNK(current_mode):
        raise _error(ErrorKind.CONFLICT, "current index path is not a symlink", current)
    temporary = output / f".mypypi-current-{secrets.token_hex(8)}"
    failure: BaseException | None = None
    temporary_created = False
    try:
        temporary.symlink_to(Path("generations") / generation)
        temporary_created = True
        os.replace(temporary, current)
        root_fd = os.open(output, os.O_RDONLY)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
    except OSError as cause:
        failure = _error(ErrorKind.IO, "could not promote package index generation", current)
        raise failure from cause
    finally:
        if temporary_created:
            _remove_temporary(temporary, failure)


def _current_items(output: Path) -> tuple[IndexArtifact, ...]:
    current = output / "current"
    try:
        current_mode = current.lstat().st_mode
    except FileNotFoundError:
        return ()
    except OSError as cause:
        raise _error(ErrorKind.IO, "could not inspect current index path", current) from cause
    if not stat.S_ISLNK(current_mode):
        raise _error(ErrorKind.CONFLICT, "current index path is not a symlink", current)
    link = Path(os.readlink(current))
    if (
        link.is_absolute()
        or len(link.parts) != 2
        or link.parts[0] != "generations"
        or len(link.parts[1]) != 64
        or any(character not in "0123456789abcdef" for character in link.parts[1])
    ):
        raise _error(ErrorKind.INTEGRITY, "published index has an invalid current link", current)
    generation = output / link
    manifest_path = generation / "manifest.json"
    try:
        generation_mode = generation.lstat().st_mode
        if stat.S_ISLNK(generation_mode) or not stat.S_ISDIR(generation_mode):
            raise ValueError("current generation is not a real directory")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as cause:
        raise _error(ErrorKind.INTEGRITY, "could not read current publication manifest", manifest_path) from cause
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "artifacts"}
        or manifest["schema_version"] != 1
        or not isinstance(manifest["artifacts"], list)
    ):
        raise _error(ErrorKind.INTEGRITY, "current publication manifest has an unknown schema", manifest_path)
    wheels = output / "wheels"
    try:
        wheels_mode = wheels.lstat().st_mode
    except OSError as cause:
        raise _error(ErrorKind.INTEGRITY, "current wheel storage is missing", wheels) from cause
    if stat.S_ISLNK(wheels_mode) or not stat.S_ISDIR(wheels_mode):
        raise _error(ErrorKind.INTEGRITY, "current wheel storage is not a real directory", wheels)
    artifacts = []
    seen: set[str] = set()
    for row in manifest["artifacts"]:
        try:
            fields = {
                "filename", "name", "version", "sha256", "metadata_sha256",
                "requires_python", "yanked", "targets",
            }
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError("manifest artifact has unknown or missing fields")
            filename = row["filename"]
            if not isinstance(filename, str) or Path(filename).name != filename:
                raise ValueError("invalid wheel filename")
            identity = WheelIdentity.parse(filename)
            if row["name"] != identity.package.name or row["version"] != str(identity.package.version):
                raise ValueError("manifest identity differs from wheel filename")
            if filename in seen:
                raise ValueError(f"duplicate wheel filename {filename}")
            seen.add(filename)
            checksum = row["sha256"]
            metadata_checksum = row["metadata_sha256"]
            if (
                not isinstance(checksum, str)
                or len(checksum) != 64
                or any(c not in "0123456789abcdef" for c in checksum)
            ):
                raise ValueError("invalid wheel hash")
            if (
                not isinstance(metadata_checksum, str)
                or len(metadata_checksum) != 64
                or any(c not in "0123456789abcdef" for c in metadata_checksum)
            ):
                raise ValueError("invalid metadata hash")
            wheel = wheels / filename
            if not stat.S_ISREG(wheel.lstat().st_mode) or _file_sha256(wheel) != checksum:
                raise ValueError(f"published wheel is missing or changed: {filename}")
            sidecar = wheels / f"{filename}.metadata"
            if not stat.S_ISREG(sidecar.lstat().st_mode):
                raise ValueError(f"published metadata is missing or invalid: {filename}")
            sidecar_bytes = sidecar.read_bytes()
            if hashlib.sha256(sidecar_bytes).hexdigest() != metadata_checksum:
                raise ValueError(f"published metadata changed: {filename}")
            targets_raw = row["targets"]
            if not isinstance(targets_raw, list) or not targets_raw or len(set(targets_raw)) != len(targets_raw):
                raise ValueError("invalid validation targets")
            targets = set()
            for value in targets_raw:
                if not isinstance(value, str):
                    raise ValueError("invalid validation target")
                python_text, separator, platform_name = value.partition(":")
                if not separator or not platform_name:
                    raise ValueError("invalid validation target")
                targets.add(Target(Version(python_text), platform_name))
            artifact = ValidatedArtifact(identity, wheel, checksum, frozenset(targets))
            item = prepare_index_artifacts((artifact,))[0]
            if item.metadata != sidecar_bytes or item.requires_python != row["requires_python"]:
                raise ValueError(f"published metadata disagrees with manifest: {filename}")
            reason = row["yanked"]
            if reason is not None:
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("invalid yanked reason")
            artifacts.append(replace(item, yanked_reason=reason))
        except (KeyError, TypeError, ValueError, OSError, MypypiError) as cause:
            if isinstance(cause, MypypiError):
                raise
            raise _error(ErrorKind.INTEGRITY, f"invalid published artifact: {cause}", manifest_path) from cause
    return tuple(artifacts)


def published_artifacts(output: Path) -> tuple[ValidatedArtifact, ...]:
    output = Path(os.path.abspath(output))
    try:
        mode = output.lstat().st_mode
    except FileNotFoundError:
        return ()
    except OSError as cause:
        raise _error(ErrorKind.IO, "could not inspect published index", output) from cause
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise _error(ErrorKind.INVALID_INPUT, "published index root must be a real directory", output)
    return tuple(item.artifact for item in _current_items(output))


def publish(
    artifacts: Iterable[ValidatedArtifact],
    output: Path,
    *,
    yanked: Mapping[str, str | None] | None = None,
) -> Path:
    validated = tuple(artifacts)
    if any(type(artifact) is not ValidatedArtifact for artifact in validated):
        raise _error(ErrorKind.INVALID_INPUT, "publication accepts only ValidatedArtifact values")
    reasons = dict(yanked) if yanked is not None else {}
    for filename, reason in reasons.items():
        if not isinstance(filename, str) or not filename:
            raise _error(ErrorKind.INVALID_INPUT, "yanked metadata filenames must be non-empty strings")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise _error(ErrorKind.INVALID_INPUT, "yanked reason must be a non-empty string")
    new_filenames = {artifact.identity.filename for artifact in validated}
    new_items = prepare_index_artifacts(
        validated,
        {
            filename: reason
            for filename, reason in reasons.items()
            if filename in new_filenames and reason is not None
        },
    )
    output = _ensure_output_root(output)
    with _ProcessLock(output / ".mypypi.lock", blocking=True):
        _recover_staging(output)
        current_items = _current_items(output)
        merged: dict[str, IndexArtifact] = {
            item.artifact.identity.filename: item for item in current_items
        }
        for item in new_items:
            filename = item.artifact.identity.filename
            existing = merged.get(filename)
            if existing is None:
                merged[filename] = item
                continue
            if existing.artifact.sha256 != item.artifact.sha256:
                raise _error(
                    ErrorKind.CONFLICT,
                    "wheel filename already exists with different bytes",
                    output / "wheels" / filename,
                    item,
                )
            if existing.metadata != item.metadata:
                raise _error(
                    ErrorKind.CONFLICT,
                    "wheel metadata differs for identical wheel bytes",
                    output / "wheels" / filename,
                    item,
                )
            updated = replace(
                item,
                artifact=replace(
                    item.artifact,
                    targets=item.artifact.targets | existing.artifact.targets,
                ),
            )
            updated = replace(
                updated,
                yanked_reason=(
                    reasons[filename]
                    if filename in reasons
                    else existing.yanked_reason
                ),
            )
            merged[filename] = updated
        for filename, reason in reasons.items():
            if filename in merged and filename not in new_filenames:
                merged[filename] = replace(merged[filename], yanked_reason=reason)
        unknown_updates = reasons.keys() - merged.keys()
        if unknown_updates:
            raise _error(
                ErrorKind.INVALID_INPUT,
                f"yanked metadata names unknown wheels: {', '.join(sorted(unknown_updates))}",
            )
        items = tuple(merged[name] for name in sorted(merged))
        for item in items:
            _store_wheel(output, item)
        manifest = _manifest(items)
        generation = _write_generation(output, items, manifest)
        if (output / "current").is_symlink() and os.readlink(output / "current") == f"generations/{generation}":
            return output / "current"
        _promote(output, generation)
    return output / "current"
