from __future__ import annotations

import signal
import subprocess
import sys

import pytest

import mypypi.process_policy as process_policy


def test_uv_environment_removes_policy_overrides_and_keeps_transport_settings():
    source = {
        "UV_DEFAULT_INDEX": "https://attacker.invalid/simple",
        "UV_FIND_LINKS": "/attacker/wheels",
        "UV_NO_BUILD_ISOLATION": "1",
        "UV_NO_VERIFY_HASHES": "1",
        "UV_OVERRIDE": "/attacker/constraints.txt",
        "PIP_TRUSTED_HOST": "attacker.invalid",
        "HTTPS_PROXY": "https://proxy.example",
        "SSL_CERT_FILE": "/certs/company.pem",
        "UV_CACHE_DIR": "/cache/uv",
    }

    result = process_policy.uv_environment(source)

    assert set(result) == {"HTTPS_PROXY", "SSL_CERT_FILE", "UV_CACHE_DIR"}


def test_redacted_diagnostic_hides_url_credentials_and_bounds_output():
    diagnostic = process_policy.redacted_diagnostic(
        "https://name:secret@example.test/simple"
    )
    bounded = process_policy.redacted_diagnostic("x" * 40, limit=30)

    assert diagnostic == "https://[credentials]@example.test/simple"
    assert "secret" not in diagnostic
    assert bounded == "x" * 30


def test_run_process_sends_input_to_child_stdin():
    result = process_policy.run_process(
        (sys.executable, "-c", "import sys; print(sys.stdin.read())"),
        input="request input",
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )

    assert result.stdout == "request input\n"


def test_timeout_stops_the_child_process_group(monkeypatch):
    killed = []

    class TimedOutProcess:
        pid = 123
        returncode = None

        def __init__(self):
            self.first_communication = True

        def communicate(self, *, input=None, timeout=None):
            if self.first_communication:
                self.first_communication = False
                raise subprocess.TimeoutExpired(("uv",), timeout)
            return "", ""

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -signal.SIGTERM
            return self.returncode

    process = TimedOutProcess()
    monkeypatch.setattr(process_policy.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(process_policy.os, "killpg", lambda pid, number: killed.append((pid, number)))

    with pytest.raises(subprocess.TimeoutExpired):
        process_policy.run_process(("uv",), timeout=1)

    assert killed == [(123, signal.SIGTERM)]


def test_keyboard_interrupt_stops_the_child_process_group(monkeypatch):
    killed = []

    class InterruptedProcess:
        pid = 456
        returncode = None

        def communicate(self, *, input=None, timeout=None):
            raise KeyboardInterrupt

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -signal.SIGTERM
            return self.returncode

    process = InterruptedProcess()
    monkeypatch.setattr(process_policy.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(process_policy.os, "killpg", lambda pid, number: killed.append((pid, number)))

    with pytest.raises(KeyboardInterrupt):
        process_policy.run_process(("uv",), timeout=10)

    assert killed == [(456, signal.SIGTERM)]
