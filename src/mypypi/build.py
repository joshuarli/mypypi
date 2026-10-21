from __future__ import annotations

import hashlib
import os
import platform
import secrets
import shlex
import shutil
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from .acquire import (
    Release,
    ReleaseFile,
    compatible_wheels,
    download_artifact,
    validate_wheel_architectures,
)
from .models import (
    ErrorKind,
    FailureContext,
    FailureStage,
    MypypiError,
    PackageVersion,
    Target,
)
from .platforms import MACOS_DEPLOYMENT_TARGET, repair_command

_BINARY_EXTENSIONS = frozenset({".c", ".cc", ".cpp", ".cxx", ".pxd", ".pxi", ".pyx", ".go", ".rs"})
_BINARY_SUFFIXES = (".so", ".dylib", ".pyd")


def require_native_target(target: Target) -> None:
    host_family = (
        "macos"
        if os.sys.platform == "darwin"
        else "linux"
        if os.sys.platform.startswith("linux")
        else os.sys.platform
    )
    host_architecture = platform.machine().lower()
    if host_family == "macos":
        host_architecture = {"aarch64": "arm64"}.get(host_architecture, host_architecture)
    elif host_family == "linux":
        host_architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(
            host_architecture, host_architecture
        )
    expected = f"{host_family}-{host_architecture}"
    if target.platform != expected:
        raise MypypiError(
            ErrorKind.INCOMPATIBLE,
            f"native builds require a matching host; host is {expected}, target is {target.platform}",
            FailureContext(FailureStage.BUILD, target=target),
        )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    parts = name.rstrip("/").split("/")
    return (
        not path.is_absolute()
        and bool(parts)
        and all(part not in {"", ".", ".."} for part in parts)
        and ":" not in parts[0]
        and "\\" not in name
        and "\x00" not in name
    )


def likely_binary(sdist: Path, ignored_paths: tuple[str, ...] = ()) -> str | None:
    ignored = set(ignored_paths)
    extensions = set()
    setup_py = bytearray()
    try:
        if zipfile.is_zipfile(sdist):
            with zipfile.ZipFile(sdist) as archive:
                members = archive.infolist()
                if len({item.filename for item in members}) != len(members):
                    raise ValueError("source archive contains duplicate paths")
                for item in members:
                    if not _safe_member(item.filename):
                        raise ValueError(f"unsafe sdist member: {item.filename}")
                    mode = item.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise ValueError(f"unsupported source archive member: {item.filename}")
                    name = item.filename
                    if "/test/" in name or "/tests/" in name or name in ignored:
                        continue
                    if name.endswith("/setup.py"):
                        setup_py.extend(archive.read(item))
                    extension = Path(name).suffix.lower()
                    if extension in _BINARY_EXTENSIONS:
                        extensions.add(extension)
        else:
            with tarfile.open(sdist, "r:*") as archive:
                members = archive.getmembers()
                if len({item.name for item in members}) != len(members):
                    raise ValueError("source archive contains duplicate paths")
                for item in members:
                    if not _safe_member(item.name):
                        raise ValueError(f"unsafe sdist member: {item.name}")
                    if not item.isfile() and not item.isdir():
                        raise ValueError(f"unsupported source archive member: {item.name}")
                    if item.isdir():
                        continue
                    name = item.name
                    if "/test/" in name or "/tests/" in name or name in ignored:
                        continue
                    if name.endswith("/setup.py"):
                        source = archive.extractfile(item)
                        if source is not None:
                            with source:
                                setup_py.extend(source.read())
                    extension = Path(name).suffix.lower()
                    if extension in _BINARY_EXTENSIONS:
                        extensions.add(extension)
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as cause:
        raise ValueError(f"cannot inspect source archive {sdist}: {cause}") from cause
    if extensions:
        return f"source archive contains compiled-source files: {', '.join(sorted(extensions))}"
    if b"cffi_modules" in setup_py:
        return "source archive setup.py declares cffi_modules"
    return None


def produced_binary(wheel: Path) -> bool:
    try:
        with zipfile.ZipFile(wheel) as archive:
            for member in _wheel_members(archive):
                if member.filename.endswith(_BINARY_SUFFIXES) or ".so." in member.filename:
                    return True
    except (OSError, zipfile.BadZipFile) as cause:
        raise ValueError(f"cannot inspect built wheel {wheel}: {cause}") from cause
    return False


def _wheel_members(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    members = tuple(archive.infolist())
    if len({member.filename for member in members}) != len(members):
        raise ValueError("wheel contains duplicate archive paths")
    for member in members:
        if not _safe_member(member.filename):
            raise ValueError(f"unsafe wheel member: {member.filename}")
        mode = member.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ValueError(f"wheel member is not a regular file or directory: {member.filename}")
    return members


def select_or_download(
    release: Release,
    target: Target,
    destination: Path,
    *,
    linux_repair_policy: str = "manylinux_2_28",
    allow_yanked_reason: str | None = None,
) -> Path | None:
    candidates = compatible_wheels(release, target, linux_repair_policy=linux_repair_policy)
    by_name = {file.filename: file for file in release.wheels}
    for item in candidates:
        file = by_name[item.filename]
        if file.yanked and not allow_yanked_reason:
            continue
        try:
            downloaded = download_artifact(file, destination)
        except MypypiError as cause:
            raise MypypiError(
                cause.kind,
                cause.detail,
                FailureContext(cause.context.stage, release.package, target),
            ) from cause
        validate_wheel_identity(downloaded, release.package)
        validate_wheel_architectures(downloaded, target)
        return downloaded
    return None


def validate_wheel_identity(wheel: Path, package: PackageVersion) -> None:
    from packaging.utils import canonicalize_name, parse_wheel_filename

    try:
        name, version, _, _ = parse_wheel_filename(wheel.name)
    except ValueError as cause:
        raise MypypiError(
            ErrorKind.INTEGRITY,
            f"invalid wheel filename {wheel.name}",
            FailureContext(FailureStage.ACQUISITION, package),
        ) from cause
    if canonicalize_name(str(name)) != package.name or version != package.version:
        raise MypypiError(
            ErrorKind.INTEGRITY,
            f"wheel filename {wheel.name} does not match {package.name}=={package.version}",
            FailureContext(FailureStage.ACQUISITION, package),
        )


def build_from_sdist(
    package: PackageVersion,
    target: Target,
    source: ReleaseFile,
    destination: Path,
    *,
    index_url: str,
    likely_binary_ignore: tuple[str, ...] = (),
    custom_prebuild: tuple[tuple[str, str], ...] = (),
    linux_repair_policy: str = "manylinux_2_28",
    build_constraints: Path | None = None,
    require_build_hashes: bool = False,
    env: dict[str, str] | None = None,
    runner=subprocess.run,
    opener=None,
) -> Path:
    require_native_target(target)
    if not index_url.startswith("https://"):
        raise MypypiError(
            ErrorKind.CONFIGURATION,
            "build dependency index must use HTTPS",
            FailureContext(FailureStage.CONFIGURATION, package, target),
        )
    destination = Path(os.path.abspath(destination))
    try:
        destination.mkdir(parents=True, exist_ok=True)
        destination_mode = destination.lstat().st_mode
    except OSError as cause:
        raise MypypiError(
            ErrorKind.IO,
            f"could not create build destination {destination}",
            FailureContext(FailureStage.BUILD, package, target),
        ) from cause
    if stat.S_ISLNK(destination_mode) or not stat.S_ISDIR(destination_mode):
        raise MypypiError(
            ErrorKind.INVALID_INPUT,
            f"build destination must be a real directory: {destination}",
            FailureContext(FailureStage.BUILD, package, target),
        )
    stage = destination / f".mypypi-build-{secrets.token_hex(16)}"
    try:
        stage.mkdir()
    except OSError as cause:
        raise MypypiError(
            ErrorKind.IO,
            f"could not create build staging directory {stage}",
            FailureContext(FailureStage.BUILD, package, target),
        ) from cause
    failure: BaseException | None = None
    failure_stage = FailureStage.BUILD
    try:
        source_path = download_artifact(source, stage / "source", **({} if opener is None else {"opener": opener}))
        suspicious = likely_binary(source_path, likely_binary_ignore)
        output = stage / "wheels"
        output.mkdir()
        build_env = dict(os.environ if env is None else env)
        for variable in tuple(build_env):
            if (
                variable.startswith(("UV_INDEX", "UV_EXTRA_INDEX", "PIP_INDEX"))
                or variable
                in {
                    "UV_DEFAULT_INDEX",
                    "UV_FIND_LINKS",
                    "PIP_EXTRA_INDEX_URL",
                    "PIP_FIND_LINKS",
                }
            ):
                build_env.pop(variable)
        build_env["ARCHFLAGS"] = ""
        build_temp = stage / "tmp"
        build_temp.mkdir()
        build_env["TMPDIR"] = str(build_temp)
        build_env["TEMP"] = str(build_temp)
        build_env["TMP"] = str(build_temp)
        build_env.pop("MACOSX_DEPLOYMENT_TARGET", None)
        if target.platform.startswith("macos-"):
            build_env["MACOSX_DEPLOYMENT_TARGET"] = ".".join(
                map(str, MACOS_DEPLOYMENT_TARGET)
            )
        if custom_prebuild:
            prefix = stage / "prefix"
            prefix.mkdir()
            for command, _ in custom_prebuild:
                command_parts = shlex.split(command)
                arguments = tuple(
                    argument.replace("{prefix}", str(prefix))
                    for argument in command_parts
                )
                if not arguments or not any("{prefix}" in part for part in command_parts):
                    raise ValueError("custom prebuild must use {prefix}")
                runner(arguments, check=True, cwd=prefix, env=build_env, capture_output=True)
            build_env["PATH"] = os.pathsep.join((str(prefix / "bin"), build_env.get("PATH", "")))
            build_env["CPPFLAGS"] = " ".join(
                filter(None, (f"-I{prefix / 'include'}", build_env.get("CPPFLAGS", "")))
            )
            build_env["LDFLAGS"] = " ".join(
                filter(None, (f"-L{prefix / 'lib'}", build_env.get("LDFLAGS", "")))
            )
            build_env["PKG_CONFIG_PATH"] = os.pathsep.join(
                filter(None, (str(prefix / "lib/pkgconfig"), build_env.get("PKG_CONFIG_PATH", "")))
            )
            build_env["LD_LIBRARY_PATH"] = os.pathsep.join(
                filter(None, (str(prefix / "lib"), build_env.get("LD_LIBRARY_PATH", "")))
            )
        command = [
            "uv",
            "build",
            "--no-config",
            "--quiet",
            "--wheel",
            f"--python={os.sys.executable}",
            f"--default-index={index_url}",
            "--index-strategy=first-index",
            f"--out-dir={output}",
        ]
        if build_constraints is not None:
            command.extend(("--build-constraints", str(build_constraints)))
        if require_build_hashes:
            command.append("--require-build-hashes")
        command.append(str(source_path))
        runner(tuple(command), check=True, cwd=stage, env=build_env, capture_output=True)
        wheels = tuple(output.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError(f"uv produced {len(wheels)} wheels for {package.name}=={package.version}")
        wheel = wheels[0]
        validate_wheel_identity(wheel, package)
        if suspicious and not produced_binary(wheel):
            raise ValueError(
                f"{package.name}=={package.version} likely contains compiled code "
                f"but produced a pure wheel: {suspicious}"
            )
        if produced_binary(wheel):
            repair_output = stage / "repaired"
            repair_output.mkdir()
            failure_stage = FailureStage.REPAIR
            repair = repair_command(target, wheel, repair_output, linux_repair_policy)
            runner(repair, check=True, cwd=stage, env=build_env, capture_output=True)
            repaired = tuple(repair_output.glob("*.whl"))
            if len(repaired) != 1:
                raise ValueError(f"repair produced {len(repaired)} wheels for {package.name}=={package.version}")
            wheel = repaired[0]
        validate_wheel_identity(wheel, package)
        validate_wheel_architectures(wheel, target, failure_stage=FailureStage.REPAIR)
        final = destination / wheel.name
        try:
            os.link(wheel, final)
        except FileExistsError as cause:
            raise MypypiError(
                ErrorKind.CONFLICT,
                f"build output already exists: {final}",
                FailureContext(FailureStage.BUILD, package, target),
            ) from cause
        wheel.unlink()
        directory_fd = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return final
    except MypypiError as cause:
        if cause.context.package is None or cause.context.target is None:
            failure = MypypiError(
                cause.kind,
                cause.detail,
                FailureContext(cause.context.stage, package, target),
            )
            raise failure from cause
        failure = cause
        raise
    except (OSError, subprocess.SubprocessError, ValueError, tarfile.TarError, zipfile.BadZipFile) as cause:
        detail = f"could not {failure_stage.value} {package.name}=={package.version}: {cause}"
        stderr = getattr(cause, "stderr", None)
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        if isinstance(stderr, str) and stderr.strip():
            detail = f"{detail}: {stderr.strip()}"
        failure = MypypiError(
            ErrorKind.BUILD,
            detail,
            FailureContext(failure_stage, package, target),
        )
        raise failure from cause
    except BaseException as cause:
        failure = cause
        raise
    finally:
        try:
            mode = stage.lstat().st_mode
            if stat.S_ISDIR(mode):
                shutil.rmtree(stage)
            else:
                stage.unlink()
        except FileNotFoundError:
            pass
        except OSError as cause:
            if failure is None:
                raise MypypiError(
                    ErrorKind.IO,
                    f"could not remove build staging directory {stage}",
                    FailureContext(FailureStage.BUILD, package, target),
                ) from cause
            failure.add_note(f"could not remove build staging directory {stage}: {cause}")
