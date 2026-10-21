import re
import tomllib
from pathlib import Path

from mypypi.platforms import MACOS_DEPLOYMENT_TARGET

ROOT = Path(__file__).resolve().parents[1]


def configured_platforms():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return tuple(project["tool"]["mypypi"]["targets"]["platforms"])


def readme_platforms():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pattern = (
        r"(?ms)^## Plan and build wheels\n.*?"
        r"^\| Platform \| Wheel OS floor \| Native build host \|\n(.*?)^\n"
    )
    section = re.search(pattern, readme)
    assert section is not None
    return tuple(re.findall(r"(?m)^\| `([^`]+)` \|", section.group(1)))


def test_readme_platforms_match_project_configuration():
    platforms = configured_platforms()

    assert len(platforms) == len(set(platforms))
    assert readme_platforms() == platforms


def test_readme_macos_floor_matches_build_policy():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    deployment_target = ".".join(map(str, MACOS_DEPLOYMENT_TARGET))

    assert f"| `macos-x86_64` | macOS {deployment_target} |" in readme
    assert f"| `macos-arm64` | macOS {deployment_target} |" in readme


def test_workflows_have_read_only_permissions_and_no_artifact_uploads():
    workflows = tuple((ROOT / ".github" / "workflows").glob("*.yml"))

    assert {"lint.yml", "test.yml"} <= {path.name for path in workflows}
    for workflow in workflows:
        source = workflow.read_text(encoding="utf-8")
        assert re.search(r"(?m)^permissions:\n  contents: read$", source)
        assert "contents: write" not in source
        for forbidden in (
            "actions/upload-artifact",
            "mypypi publish",
            "uv publish",
        ):
            assert forbidden not in source
