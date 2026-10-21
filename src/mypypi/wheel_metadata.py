from __future__ import annotations

from packaging.utils import canonicalize_name

from .models import WheelIdentity


def _matches_dist_info(identity: WheelIdentity, directory: str) -> bool:
    suffix = ".dist-info"
    if not directory.endswith(suffix):
        return False
    stem = directory[: -len(suffix)]
    version = f"-{str(identity.package.version).replace('-', '_')}"
    if not stem.casefold().endswith(version.casefold()):
        return False
    name = stem[: -len(version)]
    return canonicalize_name(name) == identity.package.name
