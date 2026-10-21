import sys

import pytest
from packaging.version import Version

from mypypi.add import compile_for_target
from mypypi.models import Target


@pytest.mark.network
def test_uv_resolves_extras_and_markers_against_manylinux_228():
    packages = compile_for_target(
        (
            'requests[socks]==2.32.5 ; python_version >= "3.15" and '
            'python_full_version >= "3.15.0"',
        ),
        Target(Version("3.15"), "linux-x86_64"),
        sys.executable,
    )

    resolved = {(package.name, str(package.version)) for package in packages}
    assert ("requests", "2.32.5") in resolved
    assert any(name == "pysocks" for name, _ in resolved)
