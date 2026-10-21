from __future__ import annotations

import os
import re
import signal
import subprocess
from collections.abc import Mapping, Sequence

_UV_POLICY_VARIABLES = frozenset(
    {
        "UV_DEFAULT_INDEX",
        "UV_FIND_LINKS",
        "UV_INDEX",
        "UV_INDEX_STRATEGY",
        "UV_NO_BUILD_ISOLATION",
        "UV_NO_BUILD_ISOLATION_PACKAGE",
        "UV_NO_CONFIG",
        "UV_NO_VERIFY_HASHES",
        "UV_OVERRIDE",
        "UV_BUILD_CONSTRAINT",
        "UV_BUILD_CONSTRAINTS",
        "UV_CONSTRAINT",
        "UV_CONFIG_FILE",
        "UV_INSECURE_HOST",
        "PIP_BUILD_CONSTRAINT",
        "PIP_CONFIG_FILE",
        "PIP_CONSTRAINT",
        "PIP_EXTRA_INDEX_URL",
        "PIP_FIND_LINKS",
        "PIP_INDEX_URL",
        "PIP_TRUSTED_HOST",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
    }
)


def uv_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if source is None else source)
    for variable in tuple(environment):
        if (
            variable in _UV_POLICY_VARIABLES
            or variable.startswith(("UV_INDEX_", "UV_EXTRA_INDEX"))
            or variable.startswith(("PIP_INDEX_", "PIP_EXTRA_INDEX"))
        ):
            environment.pop(variable)
    return environment


def redacted_diagnostic(value: str, limit: int = 2000) -> str:
    value = re.sub(
        r"(?i)(https?://)[^/@\s]+@",
        r"\1[credentials]@",
        value,
    )
    return value[-limit:]


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_process(
    command: Sequence[str],
    *,
    timeout: float,
    **options: object,
) -> subprocess.CompletedProcess[str]:
    check = bool(options.pop("check", False))
    input_text = options.pop("input", None)
    capture_output = bool(options.pop("capture_output", False))
    if input_text is not None:
        if "stdin" in options:
            raise ValueError("stdin and input arguments may not both be used.")
        options["stdin"] = subprocess.PIPE
    if capture_output:
        options.setdefault("stdout", subprocess.PIPE)
        options.setdefault("stderr", subprocess.PIPE)
    process = subprocess.Popen(
        command,
        start_new_session=True,
        **options,
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired as cause:
        _stop_process_group(process)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout if stdout is not None else cause.output,
            stderr=stderr if stderr is not None else cause.stderr,
        ) from cause
    except KeyboardInterrupt:
        _stop_process_group(process)
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode,
            command,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result
