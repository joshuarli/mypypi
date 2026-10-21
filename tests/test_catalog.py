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
    assert str(historical.package.version) == "1.0"


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
