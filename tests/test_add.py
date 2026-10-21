import subprocess
from pathlib import Path

import pytest
from packaging.version import Version

import mypypi.add as add_module
from mypypi.add import (
    _PYTHON_PLATFORMS,
    add_packages,
    append_entries,
    parse_pins,
    resolve_dependencies,
)
from mypypi.catalog import parse_catalog
from mypypi.config import load_project_settings
from mypypi.models import ErrorKind, MypypiError, Target

TARGETS = tuple(
    Target(Version("3.15"), platform)
    for platform in (
        "linux-x86_64",
        "linux-aarch64",
        "macos-x86_64",
        "macos-arm64",
    )
)


def test_add_requires_exact_pinned_input():
    assert parse_pins(("Demo_Pkg==1.2.0",))[0].name == "demo-pkg"
    with pytest.raises(MypypiError, match="exact name==version"):
        parse_pins(("demo-pkg>=1",))
    with pytest.raises(MypypiError, match="conflicting requested pins") as raised:
        parse_pins(("demo-pkg==1.0", "Demo_Pkg==2.0"))
    assert raised.value.kind is ErrorKind.CONFLICT


def test_uv_platform_mapping_covers_project_targets():
    settings = load_project_settings()
    assert set(_PYTHON_PLATFORMS) == set(settings.platforms)
    assert len(set(_PYTHON_PLATFORMS.values())) == len(settings.platforms)


def test_dependency_resolution_uses_python_315_and_each_configured_platform():
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        resolved = {
            "x86_64-unknown-linux-gnu": "base-lib==1.0\nlinux-only==2.0\n",
            "aarch64-unknown-linux-gnu": "base-lib==1.0\nlinux-only==2.0\n",
            "x86_64-apple-darwin": "base-lib==1.0\nmac-only==3.0\n",
            "aarch64-apple-darwin": "base-lib==1.0\nmac-only==3.0\n",
        }
        return subprocess.CompletedProcess(command, 0, resolved[command[-3].split("=", 1)[1]], "")

    packages = resolve_dependencies(("root==1.0",), TARGETS, "/python/3.15", run)
    assert [(package.name, str(package.version)) for package in packages] == [
        ("base-lib", "1.0"),
        ("linux-only", "2.0"),
        ("mac-only", "3.0"),
        ("root", "1.0"),
    ]
    assert len(calls) == 4
    assert {call[0][call[0].index("--python-version=3.15")] for call in calls} == {
        "--python-version=3.15"
    }
    assert {
        next(arg.split("=", 1)[1] for arg in call[0] if arg.startswith("--python-platform="))
        for call in calls
    } == set(_PYTHON_PLATFORMS.values())
    assert all(call[1]["input"] == "root==1.0" for call in calls)


def test_dependency_resolution_rejects_cross_platform_version_conflicts():
    def run(command, **kwargs):
        platform = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--python-platform="))
        version = "1.0" if platform.endswith("linux-gnu") else "2.0"
        return subprocess.CompletedProcess(command, 0, f"shared=={version}\n", "")

    with pytest.raises(MypypiError, match="conflicts across configured platforms") as raised:
        resolve_dependencies(("root==1.0",), TARGETS, "/python/3.15", run)
    assert raised.value.kind is ErrorKind.CONFLICT


def test_dependency_resolution_rejects_uv_failures():
    def run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "uv", stderr="no solution")

    with pytest.raises(MypypiError, match="resolution failed") as raised:
        resolve_dependencies(("root==1.0",), TARGETS[:1], "/python/3.15", run)
    assert raised.value.kind is ErrorKind.DEPENDENCY


def test_add_appends_without_rewriting_existing_comments_and_tables():
    source = '''# keep this note
[[packages]]
name = "base-lib"
version = "1.0"
[packages.validation]
validate_skip_imports = { "base._native" = "known platform import" }
'''
    catalog = parse_catalog(source)
    updated = append_entries(
        source,
        parse_pins(("base-lib==1.0", "new-lib==2.0")),
        catalog,
    )
    assert updated.startswith(source)
    assert updated.endswith('[[packages]]\nname = "new-lib"\nversion = "2.0"\n')
    assert append_entries(updated, parse_pins(("new-lib==2.0",)), parse_catalog(updated)) == updated


def test_catalog_path_add_preserves_comments_and_does_not_rewrite_if_unchanged(tmp_path: Path):
    path = tmp_path / "packages.toml"
    path.write_text("# original\npackages = []\n", encoding="utf-8")

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "root==1.0\nleaf==2.0\n", "")

    added = add_packages(path, ("root==1.0",), TARGETS[:1], "/python/3.15", run)
    updated = path.read_text(encoding="utf-8")
    assert "# original" in updated
    assert updated.startswith("# original\n")
    assert [(package.name, str(package.version)) for package in added] == [("leaf", "2.0"), ("root", "1.0")]
    assert add_packages(path, ("root==1.0",), TARGETS[:1], "/python/3.15", run) == added
    assert path.read_text(encoding="utf-8") == updated


def test_catalog_update_failure_preserves_the_existing_file(tmp_path: Path, monkeypatch):
    path = tmp_path / "packages.toml"
    original = "# keep this note\npackages = []\n"
    path.write_text(original, encoding="utf-8")

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "root==1.0\n", "")

    def fail_replace(*args):
        raise OSError("injected replace failure")

    monkeypatch.setattr(add_module.os, "replace", fail_replace)
    with pytest.raises(MypypiError, match="could not update catalog"):
        add_packages(path, ("root==1.0",), TARGETS[:1], "/python/3.15", run)

    assert path.read_text(encoding="utf-8") == original
    assert tuple(tmp_path.glob("*.tmp")) == ()
