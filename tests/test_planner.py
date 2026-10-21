import pytest
from packaging.version import Version

from mypypi.catalog import parse_catalog
from mypypi.models import (
    AlreadyPublished,
    ArtifactConflict,
    BuildSdist,
    DownloadWheel,
    ExcludedTarget,
    MypypiError,
    PackageVersion,
    PendingValidation,
    Target,
    WheelIdentity,
)
from mypypi.planner import ArtifactRecord, UpstreamSdist, UpstreamWheel, plan

TARGET = Target(Version("3.15"), "linux-x86_64")
PACKAGE = PackageVersion.parse("demo-pkg", "1.0")
WHEEL = WheelIdentity.parse("demo_pkg-1.0-py3-none-any.whl")


def test_plan_distinguishes_all_six_outcomes():
    catalog = parse_catalog('''
[[packages]]
name = "demo-pkg"
version = "1.0"
[packages.exclude_platforms]
linux-aarch64 = "upstream does not support this platform"
''')
    targets = tuple(
        Target(Version("3.15"), platform)
        for platform in (
            "linux-x86_64",
            "macos-x86_64",
            "macos-arm64",
            "linux-aarch64",
            "windows-x86_64",
            "windows-aarch64",
        )
    )
    package = catalog.entries[0].package
    pending_wheel = WheelIdentity.parse("demo_pkg-1.0-cp315-cp315-macosx_15_0_arm64.whl")
    published = {(package, targets[0]): ArtifactRecord(WHEEL, "a" * 64)}
    pending = {(package, targets[4]): ArtifactRecord(pending_wheel, "b" * 64)}
    upstream = {
        (package, targets[1]): (
            UpstreamWheel(
                WheelIdentity.parse(
                    "demo_pkg-1.0-cp315-cp315-manylinux_2_28_x86_64.whl"
                ),
                "https://files.example/demo.whl",
                "c" * 64,
            ),
        ),
        (package, targets[2]): (UpstreamSdist("https://files.example/demo.tar.gz", "d" * 64),),
        (package, targets[3]): (),
        (package, targets[5]): (),
    }
    upstream[(package, targets[0])] = ()
    upstream[(package, targets[5])] = (UpstreamWheel(WHEEL, "https://files.example/collision.whl", "b" * 64),)
    outcomes = plan(catalog, targets, published, upstream, pending)
    assert isinstance(outcomes[0], AlreadyPublished)
    assert isinstance(outcomes[1], DownloadWheel)
    assert isinstance(outcomes[2], BuildSdist)
    assert isinstance(outcomes[3], ExcludedTarget)
    assert isinstance(outcomes[4], PendingValidation)
    assert isinstance(outcomes[5], ArtifactConflict)
    assert outcomes[3].reason == "upstream does not support this platform"


def test_unchanged_catalog_has_an_empty_plan():
    assert plan(parse_catalog("packages = []"), (TARGET,), {}, {}) == ()


def test_plan_requires_an_upstream_artifact_when_not_excluded_or_published():
    catalog = parse_catalog('[[packages]]\nname = "demo-pkg"\nversion = "1.0"\n')
    with pytest.raises(MypypiError, match="no compatible upstream"):
        plan(catalog, (TARGET,), {}, {})


def test_plan_rejects_exclusions_outside_configured_platforms():
    catalog = parse_catalog('''
[[packages]]
name = "demo-pkg"
version = "1.0"
[packages.exclude_platforms]
windows-x86_64 = "unsupported"
''')
    with pytest.raises(MypypiError, match="unsupported excluded platform"):
        plan(catalog, (TARGET,), {}, {})


def test_plan_detects_filename_collision_with_different_bytes():
    catalog = parse_catalog('[[packages]]\nname = "demo-pkg"\nversion = "1.0"\n')
    other_target = Target(Version("3.15"), "macos-arm64")
    local_wheel = WheelIdentity.parse("demo_pkg-1.0-py3-none-any.whl")
    outcome = plan(
        catalog,
        (TARGET,),
        {(PACKAGE, other_target): ArtifactRecord(local_wheel, "a" * 64)},
        {(PACKAGE, TARGET): (UpstreamWheel(WHEEL, "https://example/w.whl", "b" * 64),)},
    )[0]
    assert isinstance(outcome, ArtifactConflict)
    assert outcome.existing_sha256 == "a" * 64
    assert outcome.candidate_sha256 == "b" * 64
