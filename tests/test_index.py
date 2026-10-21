from __future__ import annotations

import hashlib
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from packaging.version import Version

from mypypi.index import prepare_index_artifacts, render_project_page, render_root_page
from mypypi.models import MypypiError, Target, ValidatedArtifact, WheelIdentity
from mypypi.platforms import current_platform


def wheel(tmp_path: Path, filename: str, metadata: bytes) -> ValidatedArtifact:
    identity = WheelIdentity.parse(filename)
    path = tmp_path / filename
    with zipfile.ZipFile(path, "w") as archive:
        dist_info = f"{identity.package.name.replace('-', '_')}-{identity.package.version}.dist-info/METADATA"
        archive.writestr(dist_info, metadata)
    return ValidatedArtifact(
        identity,
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
        frozenset({Target(Version("3.15"), "macos-arm64")}),
    )


def test_project_page_links_work_below_mount_prefix_and_advertise_exact_metadata(tmp_path: Path) -> None:
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
    first = render_project_page(prepared)
    assert first == render_project_page(tuple(reversed(prepared)))
    text = first.decode()
    assert text.index("sample_pkg-1.0") < text.index("sample_pkg-1.1")
    assert "../../wheels/sample_pkg-1.0-py3-none-any.whl#sha256=" in text
    assert "../../../../wheels/" not in text
    assert "data-requires-python=\"&gt;=3.15,&lt;3.16\"" in text
    assert "data-yanked=\"maintainer &lt;team@example.test&gt; &amp; &quot;held&quot;\"" in text
    metadata_hash = hashlib.sha256(
        b"Metadata-Version: 2.4\nName: sample-pkg\nVersion: 1.0\nRequires-Python: >=3.15,<3.16\n\n"
    ).hexdigest()
    assert f'data-dist-info-metadata="sha256={metadata_hash}"' in text
    assert f'data-core-metadata="sha256={metadata_hash}"' in text


def test_root_page_lists_sorted_projects_once(tmp_path: Path) -> None:
    sample = wheel(
        tmp_path,
        "sample_pkg-1.0-py3-none-any.whl",
        b"Metadata-Version: 2.4\nName: sample-pkg\nVersion: 1.0\n\n",
    )
    zebra = wheel(
        tmp_path,
        "zebra-1.0-py3-none-any.whl",
        b"Metadata-Version: 2.4\nName: zebra\nVersion: 1.0\n\n",
    )
    prepared = prepare_index_artifacts((zebra, sample,))
    page = render_root_page(prepared)
    assert page == render_root_page(tuple(reversed(prepared)))
    assert page.decode().index('href="sample-pkg/"') < page.decode().index('href="zebra/"')
    assert page.decode().count('href="sample-pkg/"') == 1


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


def test_index_keeps_patch_specific_requires_python_for_a_remote_target(tmp_path):
    artifact = wheel(
        tmp_path,
        "sample_pkg-1.0-py3-none-any.whl",
        b"Metadata-Version: 2.4\nName: sample-pkg\nVersion: 1.0\n"
        b"Requires-Python: >=3.15.1\n\n",
    )
    remote_platform = (
        "linux-aarch64"
        if current_platform() == "linux-x86_64"
        else "linux-x86_64"
    )
    remote_target = replace(
        artifact,
        targets=frozenset({Target(Version("3.15"), remote_platform)}),
    )

    prepared = prepare_index_artifacts((remote_target,))

    assert prepared[0].requires_python == ">=3.15.1"


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
