from __future__ import annotations

import platform
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from packaging.tags import Tag, compatible_tags, cpython_tags, mac_platforms
from packaging.version import InvalidVersion, Version

from .models import ErrorKind, FailureContext, FailureStage, MypypiError, Target
from .process_policy import run_process

_TARGETS = {
    "linux-x86_64": ("linux", "x86_64"),
    "linux-aarch64": ("linux", "aarch64"),
    "macos-x86_64": ("macos", "x86_64"),
    "macos-arm64": ("macos", "arm64"),
}
MACOS_DEPLOYMENT_TARGET = (15, 0)


class NativeFormatError(ValueError):
    pass


def current_platform() -> str | None:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        architecture = {"aarch64": "arm64"}.get(machine, machine)
        return f"macos-{architecture}"
    if sys.platform.startswith("linux"):
        architecture = {"arm64": "aarch64"}.get(machine, machine)
        return f"linux-{architecture}"
    return None


def target_python_version(target: Target) -> Version:
    major, minor = target.python_version.release[:2]
    return Version(f"{major}.{minor}.0")


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
        return tuple(compatible)
    return tuple(mac_platforms(version=MACOS_DEPLOYMENT_TARGET, arch=architecture))


def ordered_target_tags(target: Target, linux_repair_policy: str = "manylinux_2_28") -> tuple[Tag, ...]:
    version = tuple(target.python_version.release[:2])
    platforms = target_platform_tags(target, linux_repair_policy)
    return (*cpython_tags(version, platforms=platforms), *compatible_tags(version, platforms=platforms))


def native_architectures_from_bytes(content: bytes) -> frozenset[str]:
    if content.startswith(b"\x7fELF"):
        if len(content) < 16:
            raise NativeFormatError("truncated ELF identification")
        elf_class = content[4]
        if elf_class not in {1, 2}:
            raise NativeFormatError("invalid ELF class")
        endian = "<" if content[5] == 1 else ">" if content[5] == 2 else None
        if endian is None:
            raise NativeFormatError("invalid ELF byte order")
        if content[6] != 1:
            raise NativeFormatError("invalid ELF version")
        header_size = 52 if elf_class == 1 else 64
        if len(content) < header_size:
            raise NativeFormatError("truncated ELF header")
        machine = struct.unpack(f"{endian}H", content[18:20])[0]
        version = struct.unpack(f"{endian}I", content[20:24])[0]
        declared_size = struct.unpack(f"{endian}H", content[40:42] if elf_class == 1 else content[52:54])[0]
        if version != 1 or declared_size != header_size:
            raise NativeFormatError("invalid ELF header")
        return frozenset({{3: "x86", 62: "x86_64", 183: "aarch64", 40: "arm"}.get(machine, f"elf-{machine}")})
    macho_headers = {
        b"\xfe\xed\xfa\xce": (">", 28),
        b"\xce\xfa\xed\xfe": ("<", 28),
        b"\xfe\xed\xfa\xcf": (">", 32),
        b"\xcf\xfa\xed\xfe": ("<", 32),
    }
    macho = macho_headers.get(content[:4])
    if macho is not None:
        order, header_size = macho
        if len(content) < header_size:
            raise NativeFormatError("truncated Mach-O header")
        cpu = struct.unpack(f"{order}I", content[4:8])[0]
        architecture = {7: "x86", 0x01000007: "x86_64", 12: "arm", 0x0100000C: "arm64"}.get(
            cpu, f"mach-o-{cpu}"
        )
        return frozenset({architecture})
    fat_headers = {
        b"\xca\xfe\xba\xbe": (">", 20),
        b"\xbe\xba\xfe\xca": ("<", 20),
        b"\xca\xfe\xba\xbf": (">", 32),
        b"\xbf\xba\xfe\xca": ("<", 32),
    }
    fat = fat_headers.get(content[:4])
    if fat is not None:
        order, size = fat
        if len(content) < 8:
            raise NativeFormatError("truncated universal Mach-O header")
        count = struct.unpack(f"{order}I", content[4:8])[0]
        if count == 0 or count > 64:
            raise NativeFormatError("invalid universal Mach-O architecture count")
        fat64 = size == 32
        size = 32 if fat64 else 20
        if len(content) < 8 + count * size:
            raise NativeFormatError("truncated universal Mach-O header")
        records = [content[8 + index * size : 8 + (index + 1) * size] for index in range(count)]
        result = set()
        for record in records:
            cpu = struct.unpack(f"{order}I", record[:4])[0]
            result.add({7: "x86", 0x01000007: "x86_64", 12: "arm", 0x0100000C: "arm64"}.get(cpu, f"mach-o-{cpu}"))
        return frozenset(result)
    if content.startswith(b"MZ"):
        if len(content) < 64:
            raise NativeFormatError("truncated PE DOS header")
        offset = struct.unpack("<I", content[60:64])[0]
        if offset < 64 or offset + 6 > len(content) or content[offset : offset + 4] != b"PE\x00\x00":
            raise NativeFormatError("invalid PE header")
        machine = struct.unpack("<H", content[offset + 4 : offset + 6])[0]
        return frozenset({
            0x014C: "x86",
            0x8664: "x86_64",
            0xAA64: "arm64",
        }.get(machine, f"pe-{machine}"))
    return frozenset()


def verify_repair_tool(target: Target, *, env: dict[str, str] | None = None) -> str:
    try:
        family, architecture = _TARGETS[target.platform]
    except KeyError as cause:
        raise MypypiError(
            ErrorKind.INCOMPATIBLE,
            f"unsupported build target {target.platform}",
            FailureContext(FailureStage.REPAIR, target=target),
        ) from cause
    if family == "linux":
        distribution = "auditwheel"
        module = "auditwheel"
        options = ("--plat", "--wheel-dir")
        prerequisites = ("patchelf", "readelf")
        group = "repair-linux"
    else:
        distribution = "delocate"
        module = "delocate.cmd.delocate_wheel"
        options = ("--wheel-dir",)
        prerequisites = ("otool", "install_name_tool")
        group = "repair-macos"
    try:
        from importlib.metadata import PackageNotFoundError, version

        installed = Version(version(distribution))
    except (PackageNotFoundError, InvalidVersion) as cause:
        raise MypypiError(
            ErrorKind.DEPENDENCY,
            f"{distribution} is unavailable; provision it with `uv sync --group {group}`",
            FailureContext(FailureStage.REPAIR, target=target),
        ) from cause
    for executable in prerequisites:
        if shutil.which(executable) is None:
            raise MypypiError(
                ErrorKind.DEPENDENCY,
                f"{executable} is required by {distribution} on {target.platform}",
                FailureContext(FailureStage.REPAIR, target=target),
            )
    command = (
        (sys.executable, "-m", module, "repair", "--help")
        if family == "linux"
        else (sys.executable, "-m", module, "--help")
    )
    try:
        result = run_process(
            command,
            check=True,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as cause:
        raise MypypiError(
            ErrorKind.DEPENDENCY,
            f"could not verify {distribution} {installed} repair options",
            FailureContext(FailureStage.REPAIR, target=target),
        ) from cause
    help_text = f"{result.stdout}\n{result.stderr}"
    missing = tuple(option for option in options if option not in help_text)
    if missing:
        raise MypypiError(
            ErrorKind.DEPENDENCY,
            f"{distribution} {installed} lacks required repair options: {', '.join(missing)}",
            FailureContext(FailureStage.REPAIR, target=target),
        )
    return f"{distribution} {installed} ({target.platform}, {architecture})"


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
