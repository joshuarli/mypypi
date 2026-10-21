from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from packaging.version import Version

import mypypi.store as store
from mypypi.models import ErrorKind, FailureContext, FailureStage, MypypiError, Target, ValidatedArtifact, WheelIdentity


def artifact(
    tmp_path: Path,
    filename: str = "sample_pkg-1.0-py3-none-any.whl",
    extra: bytes = b"",
) -> ValidatedArtifact:
    path = tmp_path / filename
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "sample_pkg-1.0.dist-info/METADATA",
            b"Metadata-Version: 2.4\nName: sample-pkg\nVersion: 1.0\nRequires-Python: >=3.15\n\n",
        )
        if extra:
            archive.writestr("sample.py", extra)
    return ValidatedArtifact(
        WheelIdentity.parse(filename),
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
        frozenset({Target(Version("3.15"), "macos-arm64")}),
    )


def test_publish_creates_relative_atomic_generation_and_metadata(tmp_path: Path) -> None:
    output = tmp_path / "index"
    result = store.publish((artifact(tmp_path),), output)
    assert result == output / "current"
    generation_link = output / "current"
    assert generation_link.is_symlink()
    assert generation_link.readlink().parts[0] == "generations"
    page = generation_link / "simple" / "sample-pkg" / "index.html"
    text = page.read_text()
    assert "../../../../wheels/sample_pkg-1.0-py3-none-any.whl" in text
    wheel_path = output / "wheels" / "sample_pkg-1.0-py3-none-any.whl"
    sidecar = output / "wheels" / "sample_pkg-1.0-py3-none-any.whl.metadata"
    assert sidecar.read_bytes().startswith(b"Metadata-Version: 2.4")
    assert hashlib.sha256(wheel_path.read_bytes()).hexdigest() in text
    manifest = json.loads((generation_link / "manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["artifacts"][0]["filename"] == wheel_path.name


def test_repeated_publish_is_a_noop(tmp_path: Path) -> None:
    output = tmp_path / "index"
    source = artifact(tmp_path)
    store.publish((source,), output)
    generation = (output / "current").readlink()
    wheel_bytes = (output / "wheels" / source.identity.filename).read_bytes()
    store.publish((source,), output)
    assert (output / "current").readlink() == generation
    assert (output / "wheels" / source.identity.filename).read_bytes() == wheel_bytes
    assert len(tuple((output / "generations").iterdir())) == 1


def test_same_filename_with_different_bytes_is_a_conflict(tmp_path: Path) -> None:
    output = tmp_path / "index"
    store.publish((artifact(tmp_path),), output)
    different = artifact(tmp_path, extra=b"different")
    with pytest.raises(MypypiError) as caught:
        store.publish((different,), output)
    assert caught.value.kind is ErrorKind.CONFLICT
    assert len(tuple((output / "generations").iterdir())) == 1


def test_output_root_symlink_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(MypypiError, match="must not be a symlink"):
        store.publish((artifact(tmp_path),), link)


def test_wheel_archive_traversal_is_rejected(tmp_path: Path) -> None:
    bad = artifact(tmp_path)
    with zipfile.ZipFile(bad.path, "a") as archive:
        archive.writestr("../escape.txt", b"outside")
    bad = ValidatedArtifact(bad.identity, bad.path, hashlib.sha256(bad.path.read_bytes()).hexdigest(), bad.targets)
    with pytest.raises(MypypiError, match="escapes its root"):
        store.publish((bad,), tmp_path / "index")
    assert not (tmp_path / "escape.txt").exists()


def test_interruption_before_promotion_preserves_old_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "index"
    original = artifact(tmp_path)
    store.publish((original,), output)
    previous = (output / "current").readlink()
    monkeypatch.setattr(
        store,
        "_promote",
        lambda *_: (_ for _ in ()).throw(
            MypypiError(
                ErrorKind.IO,
                "interrupt",
                FailureContext(FailureStage.PUBLICATION),
            )
        ),
    )
    with pytest.raises(MypypiError):
        store.publish((original,), output, yanked={original.identity.filename: "withdrawn"})
    assert (output / "current").readlink() == previous
    assert (output / "current" / "manifest.json").exists()


def test_partial_staging_failure_preserves_old_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "index"
    source = artifact(tmp_path)
    store.publish((source,), output)
    previous = (output / "current").readlink()
    write_bytes = Path.write_bytes

    def fail_in_staging(path: Path, content: bytes) -> int:
        if path.parent.name.startswith(".mypypi-staging-"):
            raise OSError("interrupted generation write")
        return write_bytes(path, content)

    monkeypatch.setattr(Path, "write_bytes", fail_in_staging)

    with pytest.raises(MypypiError, match="stage package index generation"):
        store.publish(
            (source,), output, yanked={source.identity.filename: "withdrawn"}
        )

    assert (output / "current").readlink() == previous
    assert not tuple(output.glob(".mypypi-staging-*"))


def test_staging_owner_marker_survives_until_generation_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "index"
    source = artifact(tmp_path)
    original_replace = store.os.replace
    owner_marker_seen = []

    def interrupt_generation_rename(source_path: Path, destination: Path) -> None:
        if source_path.name.startswith(".mypypi-staging-"):
            owner_marker_seen.append((source_path / ".mypypi-owner").is_file())
            raise KeyboardInterrupt
        original_replace(source_path, destination)

    monkeypatch.setattr(store.os, "replace", interrupt_generation_rename)

    with pytest.raises(KeyboardInterrupt):
        store.publish((source,), output)

    assert owner_marker_seen == [True]
    assert not tuple(output.glob(".mypypi-staging-*"))


def test_publish_removes_a_marker_left_on_a_committed_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "index"
    source = artifact(tmp_path)
    original_unlink = Path.unlink

    def interrupt_marker_removal(path: Path, *args, **kwargs) -> None:
        if path.name == ".mypypi-owner" and path.parent.parent == output / "generations":
            raise OSError("interrupted marker removal")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupt_marker_removal)

    with pytest.raises(MypypiError, match="remove package index staging marker"):
        store.publish((source,), output)

    generation = next((output / "generations").iterdir())
    marker = generation / ".mypypi-owner"
    assert marker.read_text(encoding="ascii") == "mypypi\n"
    monkeypatch.setattr(Path, "unlink", original_unlink)

    store.publish((source,), output)

    assert not marker.exists()
    assert (output / "current" / "manifest.json").is_file()


def test_publish_preserves_non_symlink_current_path(tmp_path: Path) -> None:
    output = tmp_path / "index"
    output.mkdir()
    current = output / "current"
    current.write_text("user data", encoding="utf-8")

    with pytest.raises(MypypiError, match="current index path is not a symlink"):
        store.publish((artifact(tmp_path),), output)

    assert current.read_text(encoding="utf-8") == "user data"


def test_interruption_after_promotion_leaves_new_generation_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "index"
    source = artifact(tmp_path)
    original_promote = store._promote

    def promote_then_interrupt(root: Path, generation: str) -> None:
        original_promote(root, generation)
        raise MypypiError(ErrorKind.IO, "interrupt", FailureContext(FailureStage.PUBLICATION))

    monkeypatch.setattr(store, "_promote", promote_then_interrupt)
    with pytest.raises(MypypiError):
        store.publish((source,), output)
    visible_generation = (output / "current").readlink()
    assert (output / "current" / "manifest.json").exists()
    monkeypatch.setattr(store, "_promote", original_promote)
    store.publish((source,), output)
    assert (output / "current").readlink() == visible_generation


def test_recovery_removes_only_owned_staging_directories(tmp_path: Path) -> None:
    output = tmp_path / "index"
    output.mkdir()
    abandoned = output / ".mypypi-staging-owned"
    abandoned.mkdir()
    (abandoned / ".mypypi-owner").write_text("mypypi\n")
    unrelated = output / ".mypypi-staging-user"
    unrelated.mkdir()
    (unrelated / "keep").write_text("user")
    store.publish((artifact(tmp_path),), output)
    assert not abandoned.exists()
    assert (unrelated / "keep").read_text() == "user"


def test_process_lock_reports_conflict(tmp_path: Path) -> None:
    output = tmp_path / "index"
    output.mkdir()
    with store._ProcessLock(output / ".mypypi.lock"):
        with pytest.raises(MypypiError) as caught:
            store.publish((artifact(tmp_path),), output)
    assert caught.value.kind is ErrorKind.CONFLICT


def test_existing_generation_page_must_still_match_its_manifest(tmp_path: Path) -> None:
    output = tmp_path / "index"
    source = artifact(tmp_path)
    store.publish((source,), output)
    page = output / "current" / "simple" / "sample-pkg" / "index.html"
    page.write_text("corrupt", encoding="utf-8")

    with pytest.raises(MypypiError, match="invalid project page"):
        store.publish((source,), output)


def test_source_wheel_change_during_publish_is_rejected(tmp_path: Path, monkeypatch) -> None:
    source = artifact(tmp_path)
    original_prepare = store.prepare_index_artifacts

    def prepare_then_change(artifacts, yanked=None):
        prepared = original_prepare(artifacts, yanked)
        source.path.write_bytes(b"changed after validation")
        return prepared

    monkeypatch.setattr(store, "prepare_index_artifacts", prepare_then_change)
    output = tmp_path / "index"

    with pytest.raises(MypypiError, match="changed while it was stored"):
        store.publish((source,), output)

    assert not (output / "current").exists()
    assert not (output / "wheels" / source.identity.filename).exists()
