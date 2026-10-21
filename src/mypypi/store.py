from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shutil
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path

from .index import IndexArtifact, prepare_index_artifacts, project_name, render_project_page
from .models import ErrorKind, FailureContext, FailureStage, MypypiError, ValidatedArtifact

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
        output.mkdir(parents=True)
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
    def __init__(self, path: Path):
        self.path = path
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
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as cause:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            raise _error(ErrorKind.CONFLICT, "another publisher holds the output lock", self.path) from cause
        except OSError as cause:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
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
        f"simple/{name}/index.html": render_project_page(
            tuple(grouped[name]), "../../../../wheels"
        )
        for name in sorted(grouped)
    }


def _verify_generation(
    generation: Path,
    manifest: bytes,
    pages: Mapping[str, bytes],
) -> None:
    manifest_path = generation / "manifest.json"
    try:
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
    digest = hashlib.sha256(manifest)
    for item in items:
        digest.update(item.metadata)
    generation = digest.hexdigest()
    generations = output / "generations"
    _ensure_directory(generations)
    final = generations / generation
    pages = _project_pages(items)
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
        for relative_path, content in pages.items():
            page = staging / relative_path
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_bytes(content)
        (staging / "manifest.json").write_bytes(manifest)
        for directory, _, files in os.walk(staging, topdown=False, followlinks=False):
            directory_path = Path(directory)
            for filename in files:
                file_path = directory_path / filename
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


def publish(
    artifacts: Iterable[ValidatedArtifact],
    output: Path,
    *,
    yanked: Mapping[str, str] | None = None,
) -> Path:
    validated = tuple(artifacts)
    if any(type(artifact) is not ValidatedArtifact for artifact in validated):
        raise _error(ErrorKind.INVALID_INPUT, "publication accepts only ValidatedArtifact values")
    reasons = dict(yanked) if yanked is not None else None
    items = prepare_index_artifacts(validated, reasons)
    output = _ensure_output_root(output)
    with _ProcessLock(output / ".mypypi.lock"):
        _recover_staging(output)
        for item in items:
            _store_wheel(output, item)
        manifest = _manifest(items)
        generation = _write_generation(output, items, manifest)
        if (output / "current").is_symlink() and os.readlink(output / "current") == f"generations/{generation}":
            return output / "current"
        _promote(output, generation)
    return output / "current"
