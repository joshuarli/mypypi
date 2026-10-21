import pytest

from mypypi.catalog import parse_catalog
from mypypi.models import MypypiError


def test_catalog_accepts_exact_historical_versions_and_optional_settings():
    catalog = parse_catalog('''
[[packages]]
name = "sample-pkg"
version = "2.0"
[packages.build]
likely_binary_ignore = { "src/generated.c" = "vendored generated code is not compiled" }
custom_prebuild = { "python build_native.py --prefix {prefix}" = "upstream requires a generated native header" }
[packages.validation]
validate_extras = { "test" = "exercise the upstream test extra" }
validate_incorrect_missing_deps = { "legacy-lib" = "upstream omits an optional runtime dependency" }
validate_skip_imports = { "sample._native" = "requires an unavailable host library" }
[packages.exclude_platforms]
linux-aarch64 = "upstream has no compatible source"

[[packages]]
name = "sample-pkg"
version = "1.0"
''')
    assert len(catalog.entries) == 2
    latest, historical = catalog.entries
    assert str(latest.package.version) == "2.0"
    assert latest.build.likely_binary_ignore == (
        ("src/generated.c", "vendored generated code is not compiled"),
    )
    assert latest.build.custom_prebuild == (
        ("python build_native.py --prefix {prefix}", "upstream requires a generated native header"),
    )
    assert latest.validation.validate_extras == (("test", "exercise the upstream test extra"),)
    assert latest.validation.validate_incorrect_missing_deps == (
        ("legacy-lib", "upstream omits an optional runtime dependency"),
    )
    assert latest.excluded_platforms == (
        ("linux-aarch64", "upstream has no compatible source"),
    )
    assert latest.platforms is None
    assert str(historical.package.version) == "1.0"


def test_catalog_preserves_custom_prebuild_command_order():
    catalog = parse_catalog('''
[[packages]]
name = "sample-pkg"
version = "2.0"
[packages.build]
custom_prebuild = {
  "python first.py --prefix {prefix}" = "prepare source",
  "python second.py --prefix {prefix}" = "compile extension"
}
''')
    assert catalog.entries[0].build.custom_prebuild == (
        ("python first.py --prefix {prefix}", "prepare source"),
        ("python second.py --prefix {prefix}", "compile extension"),
    )


def test_catalog_unions_disjoint_pin_platforms():
    catalog = parse_catalog('''
[[packages]]
name = "shared"
version = "1.0"
platforms = ["linux-x86_64"]

[[packages]]
name = "shared"
version = "1.0"
platforms = ["macos-arm64"]
''')
    assert len(catalog.entries) == 1
    assert catalog.entries[0].platforms == ("linux-x86_64", "macos-arm64")


@pytest.mark.parametrize(
    "source, detail",
    [
        ('packages = [{ name = "Sample_Pkg", version = "1.0" }]', "canonical"),
        ('packages = [{ name = "sample-pkg", version = ">=1" }]', "exact canonical version"),
        ('packages = [{ name = "sample-pkg", version = "1.0", typo = true }]', "unknown"),
        ('other = []', "unknown catalog key"),
        ('', "packages must be an array of tables"),
        (
            'packages = [{ name = "sample-pkg", version = "1.0", '
            'exclude_platforms = { "linux-x86_64" = " " } }]',
            "non-empty reason",
        ),
        (
            'packages = [{ name = "sample-pkg", version = "1.0", platforms = [] }]',
            "non-empty array",
        ),
        (
            'packages = [{ name = "sample-pkg", version = "1.0", '
            'validation = { validate_skip_imports = ["bad"] } }]',
            "map each exception",
        ),
        (
            'packages = [{ name = "sample-pkg", version = "1.0" }, '
            '{ name = "sample-pkg", version = "1.0" }]',
            "duplicate",
        ),
    ],
)
def test_catalog_rejects_malformed_entries_and_unknown_keys(source, detail):
    with pytest.raises(MypypiError, match=detail):
        parse_catalog(source)


def test_catalog_parses_empty_catalog():
    assert parse_catalog("packages = []").entries == ()


def test_catalog_requires_an_explanation_to_yank_and_explicit_false_to_clear():
    yanked = parse_catalog(
        'packages = [{ name = "sample-pkg", version = "1.0", '
        'yanked = true, yanked_reason = "security issue" }]'
    ).entries[0]
    cleared = parse_catalog(
        'packages = [{ name = "sample-pkg", version = "1.0", yanked = false }]'
    ).entries[0]

    assert yanked.yanked_reason == "security issue"
    assert not yanked.clear_yanked
    assert cleared.yanked_reason is None
    assert cleared.clear_yanked
    with pytest.raises(MypypiError, match="requires yanked_reason"):
        parse_catalog(
            'packages = [{ name = "sample-pkg", version = "1.0", yanked = true }]'
        )
