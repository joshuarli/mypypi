from __future__ import annotations

import hashlib
import ipaddress
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
from urllib.parse import unquote, urlsplit

from .acquire import (
    CompatibilityCategory,
    CompatibilityRejection,
    Release,
    ReleaseFile,
    download_artifact,
    select_or_download,
    validate_wheel_architectures,
    validate_wheel_metadata,
)
from .models import (
    ErrorKind,
    FailureContext,
    FailureStage,
    MypypiError,
    PackageVersion,
    Target,
)
from .platforms import (
    MACOS_DEPLOYMENT_TARGET,
    NativeFormatError,
    native_architectures_from_bytes,
    ordered_target_tags,
    repair_command,
    target_python_version,
    verify_repair_tool,
)
from .process_policy import redacted_diagnostic, run_process, uv_environment

_BINARY_EXTENSIONS = frozenset({".c", ".cc", ".cpp", ".cxx", ".pxd", ".pxi", ".pyx", ".go", ".rs"})
_BINARY_SUFFIXES = (".so", ".dylib", ".pyd", ".dll", ".exe")


def validate_build_index(url: str, *, strict: bool = False) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    loopback = host == "localhost"
    if host:
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    no_auth_or_query = (
        parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )
    remote_https = parsed.scheme == "https" and bool(parsed.netloc) and no_auth_or_query
    local_http = parsed.scheme == "http" and loopback and no_auth_or_query
    local_file = (
        parsed.scheme == "file"
        and parsed.netloc in {"", "localhost"}
        and Path(unquote(parsed.path)).is_absolute()
        and no_auth_or_query
    )
    if not (remote_https or local_http or local_file):
        raise MypypiError(
            ErrorKind.CONFIGURATION,
            "build dependency index must use credential-free HTTPS, loopback HTTP, or an absolute file URL",
            FailureContext(FailureStage.CONFIGURATION),
        )
    public_hosts = {"pypi.org", "pythonhosted.org"}
    if strict and remote_https and (
        host in public_hosts or any(host.endswith(f".{suffix}") for suffix in public_hosts)
    ):
        raise MypypiError(
            ErrorKind.CONFIGURATION,
            "--strict-build-deps requires a curated build dependency index",
            FailureContext(FailureStage.CONFIGURATION),
        )
    return url


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
    native_payloads = set()
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
                    with archive.open(item) as stream:
                        content = stream.read(1024 * 1024)
                    try:
                        if native_architectures_from_bytes(content):
                            native_payloads.add(name)
                    except NativeFormatError as cause:
                        raise MypypiError(
                            ErrorKind.INTEGRITY,
                            f"malformed native header in {name}: {cause}",
                            FailureContext(FailureStage.BUILD),
                        ) from cause
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
                    source = archive.extractfile(item)
                    if source is not None:
                        with source:
                            content = source.read(1024 * 1024)
                        try:
                            if native_architectures_from_bytes(content):
                                native_payloads.add(name)
                        except NativeFormatError as cause:
                            raise MypypiError(
                                ErrorKind.INTEGRITY,
                                f"malformed native header in {name}: {cause}",
                                FailureContext(FailureStage.BUILD),
                            ) from cause
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as cause:
        raise ValueError(f"cannot inspect source archive {sdist}: {cause}") from cause
    if extensions:
        return f"source archive contains compiled-source files: {', '.join(sorted(extensions))}"
    if native_payloads:
        return f"source archive contains native payloads: {', '.join(sorted(native_payloads))}"
    if b"cffi_modules" in setup_py:
        return "source archive setup.py declares cffi_modules"
    return None


def produced_binary(wheel: Path) -> bool:
    try:
        with zipfile.ZipFile(wheel) as archive:
            for member in _wheel_members(archive):
                if member.is_dir():
                    continue
                with archive.open(member) as stream:
                    content = stream.read(1024 * 1024)
                if member.filename.endswith(_BINARY_SUFFIXES) or ".so." in member.filename:
                    return True
                if native_architectures_from_bytes(content):
                    return True
    except NativeFormatError as cause:
        raise MypypiError(
            ErrorKind.INTEGRITY,
            f"malformed native header in built wheel {wheel}: {cause}",
            FailureContext(FailureStage.BUILD),
        ) from cause
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
    runner=run_process,
    opener=None,
    prebuild_cwd: Path | None = None,
) -> Path:
    require_native_target(target)
    validate_build_index(index_url)
    if build_constraints is not None:
        try:
            build_constraints = Path(build_constraints).resolve(strict=True)
            constraint_mode = build_constraints.lstat().st_mode
        except OSError as cause:
            raise MypypiError(
                ErrorKind.CONFIGURATION,
                f"could not read build constraints at {build_constraints}",
                FailureContext(FailureStage.CONFIGURATION, package, target),
            ) from cause
        if stat.S_ISLNK(constraint_mode) or not stat.S_ISREG(constraint_mode):
            raise MypypiError(
                ErrorKind.CONFIGURATION,
                f"build constraints must be a regular file: {build_constraints}",
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
    prebuild_directory = (prebuild_cwd or Path.cwd()).resolve()
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
        build_env = uv_environment(env)
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
                runner(
                    arguments,
                    check=True,
                    cwd=prebuild_directory,
                    env=build_env,
                    capture_output=True,
                    timeout=300,
                )
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
        runner(
            tuple(command),
            check=True,
            cwd=stage,
            env=build_env,
            capture_output=True,
            timeout=1800,
        )
        wheels = tuple(output.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError(f"uv produced {len(wheels)} wheels for {package.name}=={package.version}")
        wheel = wheels[0]
        validate_wheel_metadata(
            wheel,
            package,
            target,
            linux_repair_policy=linux_repair_policy,
            enforce_target_tags=False,
            failure_stage=FailureStage.BUILD,
        )
        if suspicious and not produced_binary(wheel):
            raise ValueError(
                f"{package.name}=={package.version} likely contains compiled code "
                f"but produced a pure wheel: {suspicious}"
            )
        native = produced_binary(wheel)
        if native:
            validate_wheel_architectures(wheel, target, failure_stage=FailureStage.BUILD)
            verify_repair_tool(target, env=build_env)
            repair_output = stage / "repaired"
            repair_output.mkdir()
            failure_stage = FailureStage.REPAIR
            repair = repair_command(target, wheel, repair_output, linux_repair_policy)
            runner(
                repair,
                check=True,
                cwd=stage,
                env=build_env,
                capture_output=True,
                timeout=600,
            )
            repaired = tuple(repair_output.glob("*.whl"))
            if len(repaired) != 1:
                raise ValueError(f"repair produced {len(repaired)} wheels for {package.name}=={package.version}")
            wheel = repaired[0]
        validate_wheel_metadata(
            wheel,
            package,
            target,
            linux_repair_policy=linux_repair_policy,
            enforce_target_tags=True,
            failure_stage=FailureStage.REPAIR if native else FailureStage.BUILD,
        )
        if native:
            validate_wheel_architectures(wheel, target, failure_stage=FailureStage.REPAIR)
            _validate_repaired_policy(wheel, package, target, linux_repair_policy)
        final = destination / wheel.name
        try:
            os.link(wheel, final)
        except FileExistsError:
            try:
                existing_mode = final.lstat().st_mode
                same_bytes = stat.S_ISREG(existing_mode) and sha256(final) == sha256(wheel)
            except OSError as cause:
                raise MypypiError(
                    ErrorKind.IO,
                    f"could not inspect existing build output {final}",
                    FailureContext(FailureStage.BUILD, package, target),
                ) from cause
            if not same_bytes:
                raise MypypiError(
                    ErrorKind.CONFLICT,
                    f"build output contains conflicting bytes: {final}",
                    FailureContext(FailureStage.BUILD, package, target),
                )
        else:
            directory_fd = os.open(destination, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        wheel.unlink()
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
            detail = f"{detail}: {redacted_diagnostic(stderr)}"
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
            if isinstance(failure, CompatibilityRejection):
                raise MypypiError(
                    ErrorKind.IO,
                    f"could not remove rejected build staging directory {stage}",
                    FailureContext(FailureStage.BUILD, package, target),
                ) from cause
            failure.add_note(f"could not remove build staging directory {stage}: {cause}")


def _validate_repaired_policy(
    wheel: Path,
    package: PackageVersion,
    target: Target,
    linux_repair_policy: str,
) -> None:
    try:
        from packaging.utils import parse_wheel_filename

        _, _, _, tags = parse_wheel_filename(wheel.name)
        allowed = set(ordered_target_tags(target, linux_repair_policy))
        if not allowed.intersection(tags):
            raise CompatibilityRejection(
                CompatibilityCategory.REPAIR_POLICY,
                f"repaired wheel tags do not satisfy {linux_repair_policy} for {target.platform}",
                FailureContext(FailureStage.REPAIR, package, target),
            )
        if target.platform.startswith("macos-"):
            import re

            deployment_tags = tuple(
                re.fullmatch(r"macosx_(\d+)_(\d+)_(?:x86_64|arm64|universal2)", tag.platform)
                for tag in tags
            )
            deployment_tags = tuple(match for match in deployment_tags if match is not None)
            if not deployment_tags or any(
                (int(match.group(1)), int(match.group(2))) < MACOS_DEPLOYMENT_TARGET
                for match in deployment_tags
            ):
                raise CompatibilityRejection(
                    CompatibilityCategory.REPAIR_POLICY,
                    "repaired macOS wheel does not retain the "
                    f"{MACOS_DEPLOYMENT_TARGET[0]}.{MACOS_DEPLOYMENT_TARGET[1]} deployment floor",
                    FailureContext(FailureStage.REPAIR, package, target),
                )
    except CompatibilityRejection:
        raise
    except ValueError as cause:
        raise MypypiError(
            ErrorKind.INTEGRITY,
            f"repaired wheel has an invalid filename: {wheel.name}",
            FailureContext(FailureStage.REPAIR, package, target),
        ) from cause


def acquire_or_build(
    release: Release,
    target: Target,
    destination: Path,
    *,
    index_url: str,
    linux_repair_policy: str = "manylinux_2_28",
    allow_yanked_reason: str | None = None,
    likely_binary_ignore: tuple[str, ...] = (),
    custom_prebuild: tuple[tuple[str, str], ...] = (),
    build_constraints: Path | None = None,
    require_build_hashes: bool = False,
    env: dict[str, str] | None = None,
    runner=run_process,
    opener=None,
    prebuild_cwd: Path | None = None,
) -> Path:
    execution_version = target_python_version(target)
    if not release.requires_python.contains(str(execution_version), prereleases=True):
        raise CompatibilityRejection(
            CompatibilityCategory.PYTHON_VERSION,
            f"release Requires-Python excludes CPython {execution_version}",
            FailureContext(FailureStage.ACQUISITION, release.package, target),
        )
    wheel_rejection = None
    try:
        wheel = select_or_download(
            release,
            target,
            destination,
            linux_repair_policy=linux_repair_policy,
            allow_yanked_reason=allow_yanked_reason,
            **({} if opener is None else {"opener": opener}),
        )
    except CompatibilityRejection as cause:
        wheel = None
        wheel_rejection = cause
    if wheel is not None:
        return wheel
    last_rejection = None
    for source in release.sdists:
        if source.requires_python is not None and not source.requires_python.contains(
            str(execution_version), prereleases=True
        ):
            last_rejection = CompatibilityRejection(
                CompatibilityCategory.PYTHON_VERSION,
                f"source archive Requires-Python excludes CPython {execution_version}",
                FailureContext(FailureStage.ACQUISITION, release.package, target),
            )
            continue
        try:
            return build_from_sdist(
                release.package,
                target,
                source,
                destination,
                index_url=index_url,
                likely_binary_ignore=likely_binary_ignore,
                custom_prebuild=custom_prebuild,
                linux_repair_policy=linux_repair_policy,
                build_constraints=build_constraints,
                require_build_hashes=require_build_hashes,
                env=env,
                runner=runner,
                opener=opener,
                prebuild_cwd=prebuild_cwd,
            )
        except CompatibilityRejection as cause:
            last_rejection = cause
    if last_rejection is not None:
        raise last_rejection
    if wheel_rejection is not None:
        raise wheel_rejection
    raise MypypiError(
        ErrorKind.MISSING_ARTIFACT,
        f"no compatible wheel or source archive for {release.package.name}=={release.package.version}",
        FailureContext(FailureStage.ACQUISITION, release.package, target),
    )
