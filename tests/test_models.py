from pathlib import Path

import pytest
from packaging.version import Version

from mypypi.models import (
    ErrorKind,
    FailureContext,
    FailureStage,
    MypypiError,
    PackageVersion,
    Target,
    ValidatedArtifact,
    WheelIdentity,
)


def test_package_version_normalizes_distribution_name():
    assert PackageVersion.parse("Example_Pkg", "1.2.3") == PackageVersion(
        "example-pkg", Version("1.2.3")
    )


def test_package_version_rejects_noncanonical_direct_construction():
    with pytest.raises(ValueError, match="canonical"):
        PackageVersion("Example_Pkg", Version("1.2.3"))


def test_wheel_identity_parses_filename_and_empty_build_tag():
    identity = WheelIdentity.parse("example_pkg-1.2.3-py3-none-any.whl")
    assert identity.filename == "example_pkg-1.2.3-py3-none-any.whl"
    assert identity.package == PackageVersion.parse("example-pkg", "1.2.3")
    assert identity.build_tag is None
    assert {str(tag) for tag in identity.tags} == {"py3-none-any"}


def test_validated_artifact_requires_a_hash_and_covered_target():
    identity = WheelIdentity.parse("example_pkg-1.2.3-py3-none-any.whl")
    target = Target(Version("3.15"), "macos-arm64")
    with pytest.raises(ValueError, match="SHA-256"):
        ValidatedArtifact(identity, Path("example.whl"), "bad", frozenset({target}))
    with pytest.raises(ValueError, match="at least one target"):
        ValidatedArtifact(identity, Path("example.whl"), "a" * 64, frozenset())


def test_mypypi_error_keeps_stage_as_structured_context():
    error = MypypiError(
        kind=ErrorKind.CONFIGURATION,
        detail="invalid target settings",
        context=FailureContext(FailureStage.CONFIGURATION),
    )
    assert error.context.stage is FailureStage.CONFIGURATION
    assert error.detail == "invalid target settings"
