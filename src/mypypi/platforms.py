from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

from packaging.tags import Tag, compatible_tags, cpython_tags, mac_platforms

from .models import ErrorKind, FailureContext, FailureStage, MypypiError, Target

_TARGETS = {
    "linux-x86_64": ("linux", "x86_64"),
    "linux-aarch64": ("linux", "aarch64"),
    "macos-x86_64": ("macos", "x86_64"),
    "macos-arm64": ("macos", "arm64"),
}
MACOS_DEPLOYMENT_TARGET = (15, 0)


def target_platform_tags(target: Target, linux_repair_policy: str = "manylinux_2_28") -> tuple[str, ...]:
    try:
        family, architecture = _TARGETS[target.platform]
    except KeyError as cause:
        raise MypypiError(
            ErrorKind.INCOMPATIBLE,
            f"unsupported build target {target.platform}",
            FailureContext(FailureStage.BUILD, target=target),
        ) from cause
    if family == "linux":
        match = re.fullmatch(r"manylinux_2_(\d+)", linux_repair_policy)
        if match is None:
            raise ValueError("Linux repair policy must be a manylinux_2_<minor> platform tag")
        floor = int(match.group(1))
        if floor < 5:
            raise ValueError("Linux repair policy must be at least manylinux_2_5")
        compatible = [f"manylinux_2_{minor}_{architecture}" for minor in range(floor, 4, -1)]
        aliases = {17: "manylinux2014", 12: "manylinux2010", 5: "manylinux1"}
        compatible.extend(f"{name}_{architecture}" for minor, name in aliases.items() if floor >= minor)
        compatible.append(f"linux_{architecture}")
        return tuple(compatible)
    return tuple(mac_platforms(version=MACOS_DEPLOYMENT_TARGET, arch=architecture))


def ordered_target_tags(target: Target, linux_repair_policy: str = "manylinux_2_28") -> tuple[Tag, ...]:
    version = tuple(target.python_version.release[:2])
    platforms = target_platform_tags(target, linux_repair_policy)
    return (*cpython_tags(version, platforms=platforms), *compatible_tags(version, platforms=platforms))


def native_architectures_from_bytes(content: bytes) -> frozenset[str]:
    header = content[:8]
    if header.startswith(b"\x7fELF"):
        if len(header) < 6:
            raise ValueError("truncated ELF header")
        endian = "<" if header[5] == 1 else ">" if header[5] == 2 else None
        if endian is None:
            raise ValueError("invalid ELF byte order")
        if len(content) < 20:
            raise ValueError("truncated ELF header")
        machine = struct.unpack(f"{endian}H", content[18:20])[0]
        return frozenset({{3: "x86", 62: "x86_64", 183: "aarch64", 40: "arm"}.get(machine, f"elf-{machine}")})
    if header[:4] in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"}:
        if len(content) < 28:
            raise ValueError("truncated Mach-O header")
        architecture = {0x01000007: "x86_64", 0x0100000C: "arm64"}.get(
            struct.unpack("<I", header[4:8])[0],
            "mach-o-unknown",
        )
        return frozenset({architecture})
    if header[:4] in {b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}:
        order = ">" if header[:4] in {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"} else "<"
        count = struct.unpack(f"{order}I", header[4:8])[0]
        fat64 = header[:4] in {b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
        size = 32 if fat64 else 20
        if len(content) < 8 + count * size:
            raise ValueError("truncated universal Mach-O header")
        records = [content[8 + index * size : 8 + (index + 1) * size] for index in range(count)]
        result = set()
        for record in records:
            cpu = struct.unpack(f"{order}I", record[:4])[0]
            result.add({7: "x86", 0x01000007: "x86_64", 12: "arm", 0x0100000C: "arm64"}.get(cpu, f"mach-o-{cpu}"))
        return frozenset(result)
    return frozenset()


def expected_architectures(filename: str) -> frozenset[str]:
    from packaging.utils import parse_wheel_filename

    _, _, _, tags = parse_wheel_filename(filename)
    result = set()
    for tag in tags:
        platform = tag.platform
        if platform == "any":
            continue
        for name, architecture in (
            ("x86_64", "x86_64"),
            ("aarch64", "aarch64"),
            ("arm64", "arm64"),
            ("universal2", "universal2"),
            ("intel", "x86_64"),
        ):
            if platform.endswith(name):
                if architecture == "universal2":
                    result.update(("x86_64", "arm64"))
                else:
                    result.add(architecture)
                break
        else:
            raise ValueError(f"cannot determine architecture from wheel tag {platform}")
    return frozenset(result)


def repair_command(
    target: Target,
    wheel: Path,
    output: Path,
    linux_repair_policy: str,
) -> tuple[str, ...]:
    family, architecture = _TARGETS[target.platform]
    if family == "linux":
        return (
            sys.executable,
            "-mauditwheel",
            "repair",
            f"--plat={linux_repair_policy}_{architecture}",
            f"--wheel-dir={output}",
            str(wheel),
        )
    if family == "macos":
        return (sys.executable, "-mdelocate.cmd.delocate_wheel", str(wheel), "--wheel-dir", str(output))
    raise AssertionError(family)
