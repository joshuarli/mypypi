from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from packaging.version import Version

from mypypi.index import prepare_index_artifacts, render_project_page
from mypypi.models import MypypiError, Target, ValidatedArtifact, WheelIdentity


def wheel(tmp_path: Path, filename: str, metadata: bytes) -> ValidatedArtifact:
    path = tmp_path / filename
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("sample_pkg-1.0.dist-info/METADATA", metadata)
    return ValidatedArtifact(
        WheelIdentity.parse(filename),
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
        frozenset({Target(Version("3.15"), "macos-arm64")}),
    )


def test_index_is_sorted_relative_escaped_and_has_metadata_hashes(tmp_path: Path) -> None:
    one = wheel(
        tmp_path,
        "sample_pkg-1.0-py3-none-any.whl",
        b"Metadata-Version: 2.4\nName: sample-pkg\nVersion: 1.0\nRequires-Python: >=3.15,<3.16\n\n",
    )
    two = wheel(
        tmp_path,
        "sample_pkg-1.1-py3-none-any.whl",
        b"Metadata-Version: 2.4\nName: sample-pkg\nVersion: 1.1\nRequires-Python: >=3.15\n\n",
    )
    prepared = prepare_index_artifacts((two, one), {one.identity.filename: 'maintainer <team@example.test> & "held"'})
    first = render_project_page(prepared, "../../../../wheels")
    assert first == render_project_page(tuple(reversed(prepared)), "../../../../wheels")
    text = first.decode()
    assert text.index("sample_pkg-1.0") < text.index("sample_pkg-1.1")
    assert "../../../../wheels/sample_pkg-1.0-py3-none-any.whl#sha256=" in text
    assert "data-requires-python=\"&gt;=3.15,&lt;3.16\"" in text
    assert "data-yanked=\"maintainer &lt;team@example.test&gt; &amp; &quot;held&quot;\"" in text
    metadata_hash = hashlib.sha256(
        b"Metadata-Version: 2.4\nName: sample-pkg\nVersion: 1.0\nRequires-Python: >=3.15,<3.16\n\n"
    ).hexdigest()
    assert f'data-dist-info-metadata="sha256={metadata_hash}"' in text
    assert f'data-core-metadata="sha256={metadata_hash}"' in text


def test_index_rejects_missing_yanked_reason(tmp_path: Path) -> None:
    artifact = wheel(
        tmp_path,
        "sample_pkg-1.0-py3-none-any.whl",
        b"Metadata-Version: 2.4\nName: sample-pkg\nVersion: 1.0\n\n",
    )
    with pytest.raises(MypypiError, match="non-empty"):
        prepare_index_artifacts((artifact,), {artifact.identity.filename: " "})


@pytest.mark.parametrize(
    ("yanked", "message"),
    [
        ({"sample_pkg-1.0-py3-none-any.whl": None}, "non-empty"),
        ({"unknown-1.0-py3-none-any.whl": "withdrawn"}, "unknown wheels"),
    ],
)
def test_index_rejects_invalid_yanked_metadata(tmp_path, yanked, message):
    artifact = wheel(
        tmp_path,
        "sample_pkg-1.0-py3-none-any.whl",
        b"Metadata-Version: 2.4\nName: sample-pkg\nVersion: 1.0\n\n",
    )
    with pytest.raises(MypypiError, match=message):
        prepare_index_artifacts((artifact,), yanked)


def test_index_rejects_invalid_and_excluding_requires_python(tmp_path):
    for requires_python in ("not a specifier", ">=3.16"):
        artifact = wheel(
            tmp_path,
            "sample_pkg-1.0-py3-none-any.whl",
            (
                "Metadata-Version: 2.4\nName: sample-pkg\nVersion: 1.0\n"
                f"Requires-Python: {requires_python}\n\n"
            ).encode(),
        )
        with pytest.raises(MypypiError, match="Requires-Python"):
            prepare_index_artifacts((artifact,))


def test_index_rejects_duplicate_wheel_archive_paths(tmp_path):
    artifact = wheel(
        tmp_path,
        "sample_pkg-1.0-py3-none-any.whl",
        b"Metadata-Version: 2.4\nName: sample-pkg\nVersion: 1.0\n\n",
    )
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(artifact.path, "a") as archive:
            archive.writestr("sample.py", "first")
            archive.writestr("sample.py", "second")
    changed = ValidatedArtifact(
        artifact.identity,
        artifact.path,
        hashlib.sha256(artifact.path.read_bytes()).hexdigest(),
        artifact.targets,
    )
    with pytest.raises(MypypiError, match="duplicate archive paths"):
        prepare_index_artifacts((changed,))


def test_index_rejects_invalid_artifact_before_reading_yanked_metadata():
    with pytest.raises(MypypiError, match="ValidatedArtifact"):
        prepare_index_artifacts(
            (object(),), {"sample-1.0-py3-none-any.whl": "withdrawn"}
        )
