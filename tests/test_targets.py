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

[tool.mypypi.targets.linux_builder_images]
"linux-x86_64" = {{ tag = "quay.io/pypa/manylinux_2_28_x86_64:2026.10.21-1", digest = "sha256:{'a' * 64}" }}
"linux-aarch64" = {{ tag = "quay.io/pypa/manylinux_2_28_aarch64:2026.10.21-1", digest = "sha256:{'b' * 64}" }}
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
    assert tuple(image.platform for image in settings.linux_builder_images) == (
        "linux-x86_64",
        "linux-aarch64",
    )
    assert settings.linux_builder_images[0].digest == f"sha256:{'a' * 64}"


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


def test_linux_builder_images_require_immutable_digests():
    source = project().replace("sha256:" + "a" * 64, "latest")
    with pytest.raises(MypypiError, match="immutable SHA-256 digest"):
        parse_project_settings(source)


def test_linux_builder_images_must_match_linux_targets():
    source = project().replace('"linux-aarch64" = {', '"linux-other" = {')
    with pytest.raises(MypypiError, match="match the configured Linux targets"):
        parse_project_settings(source)
