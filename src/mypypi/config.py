from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from .models import ErrorKind, FailureContext, FailureStage, MypypiError, Target


@dataclass(frozen=True, slots=True)
class BuilderImage:
    platform: str
    tag: str
    digest: str


@dataclass(frozen=True, slots=True)
class ProjectSettings:
    python_specifier: SpecifierSet
    python_version: Version
    platforms: tuple[str, ...]
    linux_repair_policy: str
    linux_builder_images: tuple[BuilderImage, ...]

    def targets(self) -> tuple[Target, ...]:
        return tuple(
            Target(self.python_version, platform) for platform in self.platforms
        )


def _configuration_error(detail: str) -> MypypiError:
    return MypypiError(
        ErrorKind.CONFIGURATION,
        detail,
        FailureContext(FailureStage.CONFIGURATION),
    )


def _python_minor(python_specifier: SpecifierSet) -> Version:
    specifiers = tuple(python_specifier)
    if len(specifiers) != 2:
        raise _configuration_error(
            "project.requires-python must select exactly one Python minor release"
        )
    by_operator = {
        specifier.operator: Version(specifier.version) for specifier in specifiers
    }
    lower = by_operator.get(">=")
    upper = by_operator.get("<")
    if lower is None or upper is None:
        raise _configuration_error(
            "project.requires-python must use an inclusive lower and "
            "exclusive upper bound"
        )
    if len(lower.release) != 2 or len(upper.release) != 2:
        raise _configuration_error(
            "project.requires-python bounds must name major and minor releases"
        )
    if lower.major != upper.major or upper.minor != lower.minor + 1:
        raise _configuration_error(
            "project.requires-python must cover exactly one Python minor release"
        )
    return Version(f"{lower.major}.{lower.minor}")


def parse_project_settings(source: str) -> ProjectSettings:
    try:
        document = tomllib.loads(source)
        project = document["project"]
        python_specifier = SpecifierSet(project["requires-python"])
        python_version = _python_minor(python_specifier)
        target_settings = document["tool"]["mypypi"]["targets"]
        raw_platforms = target_settings["platforms"]
        linux_repair_policy = target_settings["linux_repair_policy"]
        raw_builder_images = target_settings["linux_builder_images"]
    except MypypiError:
        raise
    except (KeyError, TypeError, tomllib.TOMLDecodeError, ValueError) as cause:
        raise _configuration_error("invalid project target configuration") from cause
    if not isinstance(raw_platforms, list) or not raw_platforms or not all(
        isinstance(platform, str) and platform and platform.strip() == platform
        for platform in raw_platforms
    ):
        raise _configuration_error("target platforms must be non-empty names")
    if len(set(raw_platforms)) != len(raw_platforms):
        raise _configuration_error("target platforms must be unique")
    if (
        not isinstance(linux_repair_policy, str)
        or not linux_repair_policy
        or linux_repair_policy.strip() != linux_repair_policy
    ):
        raise _configuration_error("Linux repair policy must be a non-empty name")
    linux_platforms = tuple(
        platform for platform in raw_platforms if platform.startswith("linux-")
    )
    if not isinstance(raw_builder_images, dict) or set(raw_builder_images) != set(
        linux_platforms
    ):
        raise _configuration_error(
            "Linux builder images must match the configured Linux targets"
        )
    builder_images = []
    for platform_name in linux_platforms:
        image = raw_builder_images[platform_name]
        if not isinstance(image, dict) or set(image) != {"tag", "digest"}:
            raise _configuration_error(
                f"{platform_name} builder image requires only tag and digest"
            )
        tag = image["tag"]
        digest = image["digest"]
        architecture = platform_name.removeprefix("linux-")
        repository, separator, image_tag = (
            tag.rpartition(":") if isinstance(tag, str) else ("", "", "")
        )
        if (
            not isinstance(tag, str)
            or repository != f"quay.io/pypa/manylinux_2_28_{architecture}"
            or not separator
            or not image_tag
            or any(character.isspace() for character in tag)
            or "@" in tag
        ):
            raise _configuration_error(
                f"{platform_name} builder image must retain its human-readable manylinux tag"
            )
        if not isinstance(digest, str) or re.fullmatch(
            r"sha256:[0-9a-f]{64}", digest
        ) is None:
            raise _configuration_error(
                f"{platform_name} builder image requires an immutable SHA-256 digest"
            )
        builder_images.append(BuilderImage(platform_name, tag, digest))
    return ProjectSettings(
        python_specifier=python_specifier,
        python_version=python_version,
        platforms=tuple(raw_platforms),
        linux_repair_policy=linux_repair_policy,
        linux_builder_images=tuple(builder_images),
    )


def load_project_settings(path: Path = Path("pyproject.toml")) -> ProjectSettings:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as cause:
        error = MypypiError(
            ErrorKind.IO,
            f"could not read project configuration at {path}",
            FailureContext(FailureStage.CONFIGURATION),
        )
        raise error from cause
    return parse_project_settings(source)
