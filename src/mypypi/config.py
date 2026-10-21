from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from .models import ErrorKind, FailureContext, FailureStage, MypypiError, Target


@dataclass(frozen=True, slots=True)
class ProjectSettings:
    python_specifier: SpecifierSet
    python_version: Version
    platforms: tuple[str, ...]
    linux_repair_policy: str

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
        if not isinstance(target_settings, dict):
            raise TypeError("target settings must be a table")
        unknown = set(target_settings) - {"platforms", "linux_repair_policy"}
        if unknown:
            raise _configuration_error(
                f"unknown target setting: {sorted(unknown)[0]}"
            )
        raw_platforms = target_settings["platforms"]
        linux_repair_policy = target_settings["linux_repair_policy"]
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
    return ProjectSettings(
        python_specifier=python_specifier,
        python_version=python_version,
        platforms=tuple(raw_platforms),
        linux_repair_policy=linux_repair_policy,
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
