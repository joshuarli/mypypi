import pytest
from packaging.version import Version

from mypypi.config import parse_project_settings
from mypypi.models import MypypiError


def project(requires_python=">=3.15,<3.16"):
    return f'''
[project]
requires-python = "{requires_python}"

[tool.mypypi.targets]
platforms = ["linux-x86_64", "linux-aarch64", "macos-x86_64", "macos-arm64"]
linux_repair_policy = "manylinux_2_28"
'''


def test_targets_derive_python_from_project_metadata():
    settings = parse_project_settings(project())
    assert settings.python_version == Version("3.15")
    assert settings.python_specifier.contains(Version("3.15.0"))
    assert not settings.python_specifier.contains(Version("3.14"))
    assert not settings.python_specifier.contains(Version("3.16"))
    assert tuple(target.platform for target in settings.targets()) == (
        "linux-x86_64",
        "linux-aarch64",
        "macos-x86_64",
        "macos-arm64",
    )


@pytest.mark.parametrize(
    "requires_python",
    (">=3.14,<3.16", ">=3.15,<3.17", "==3.15.*"),
)
def test_target_python_must_select_one_minor(requires_python):
    with pytest.raises(MypypiError):
        parse_project_settings(project(requires_python))


def test_target_platforms_must_be_unique():
    source = project().replace('"macos-arm64"]', '"macos-arm64", "macos-arm64"]')
    with pytest.raises(MypypiError, match="must be unique"):
        parse_project_settings(source)


def test_target_platforms_cannot_be_empty():
    with pytest.raises(MypypiError, match="target platforms"):
        parse_project_settings(project().replace(
            '["linux-x86_64", "linux-aarch64", "macos-x86_64", "macos-arm64"]',
            "[]",
        ))


def test_unknown_target_settings_are_rejected():
    with pytest.raises(MypypiError, match="unknown target setting"):
        parse_project_settings(project() + 'extra_target = "ignored"\n')
