from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from base64 import urlsafe_b64encode
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

from mypypi.cli import main
from mypypi.validate import _local_platform


def _uv_environment(tmp_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith(("UV_INDEX", "UV_EXTRA_INDEX", "PIP_INDEX")) or name in {
            "UV_DEFAULT_INDEX",
            "PIP_EXTRA_INDEX_URL",
        }:
            environment.pop(name)
    environment["TMPDIR"] = str(tmp_path)
    return environment


def _write_fixture_wheel(path: Path) -> None:
    dist_info = "loopback_fixture-1.0.dist-info"
    members = {
        "loopback_fixture.py": b'value = "from the local index"\n',
        f"{dist_info}/METADATA": (
            b"Metadata-Version: 2.4\nName: loopback-fixture\nVersion: 1.0\n"
            b"Requires-Python: >=3.15,<3.16\n\n"
        ),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n\n"
        ),
    }
    record = io.StringIO()
    for name, data in members.items():
        digest = urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        record.write(f"{name},sha256={digest},{len(data)}\n")
    record.write(f"{dist_info}/RECORD,,\n")
    members[f"{dist_info}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


class _RejectingProxyHandler(BaseHTTPRequestHandler):
    attempted_targets: list[str] = []

    def do_CONNECT(self):
        self._reject_external_request()

    def do_GET(self):
        self._reject_external_request()

    def do_HEAD(self):
        self._reject_external_request()

    def log_message(self, format, *args):
        return

    def _reject_external_request(self):
        type(self).attempted_targets.append(self.path)
        self.send_error(502)


class _RecordingIndexHandler(SimpleHTTPRequestHandler):
    requested_paths: list[str] = []

    def __init__(self, *args, directory=None, prefix="", **kwargs):
        self.prefix = prefix
        super().__init__(*args, directory=directory, **kwargs)

    def do_GET(self):
        type(self).requested_paths.append(self.path)
        super().do_GET()

    def do_HEAD(self):
        type(self).requested_paths.append(self.path)
        super().do_HEAD()

    def translate_path(self, path):
        parsed = urlsplit(path)
        if self.prefix:
            if parsed.path == self.prefix or parsed.path.startswith(f"{self.prefix}/"):
                route = parsed.path[len(self.prefix):] or "/"
                path = urlunsplit(("", "", route, parsed.query, ""))
        return super().translate_path(path)


def test_uv_installs_from_local_index(tmp_path, monkeypatch):
    uv = shutil.which("uv")
    assert uv is not None
    host_platform = _local_platform()
    assert host_platform is not None
    state_dir = tmp_path / "state"
    wheel_dir = state_dir / "wheels"
    wheel_dir.mkdir(parents=True)
    wheel_path = wheel_dir / "loopback_fixture-1.0-py3-none-any.whl"
    _write_fixture_wheel(wheel_path)
    output = tmp_path / "index"
    output.mkdir()
    catalog = tmp_path / "packages.toml"
    catalog.write_text(
        '[[packages]]\nname = "loopback-fixture"\nversion = "1.0"\n',
        encoding="utf-8",
    )
    environment = _uv_environment(tmp_path)
    wheel = wheel_path
    original_wheel_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()

    _RecordingIndexHandler.requested_paths.clear()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(_RecordingIndexHandler, directory=str(output), prefix="/repo"),
    )
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    index_url = f"http://127.0.0.1:{server.server_port}/current/simple"
    proxy = None
    proxy_thread = None
    try:
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        assert main(
            [
                "--catalog",
                str(catalog),
                "--state-dir",
                str(state_dir),
                "validate",
                "--output",
                str(output),
                "--platform",
                host_platform,
            ]
        ) == 0
        publish_arguments = [
            "--catalog",
            str(catalog),
            "--state-dir",
            str(state_dir),
            "publish",
            "--output",
            str(output),
            "--platform",
            host_platform,
        ]
        assert main(publish_arguments) == 0
        generation = (output / "current").readlink()
        initial_generations = tuple(
            sorted(path.name for path in (output / "generations").iterdir())
        )
        assert hashlib.sha256(wheel.read_bytes()).hexdigest() == original_wheel_hash

        with urlopen(f"{index_url}/") as response:
            root_listing = response.read().decode()
        with urlopen(f"http://127.0.0.1:{server.server_port}/repo/current/simple/") as response:
            prefixed_root_listing = response.read().decode()
        assert 'href="loopback-fixture/"' in root_listing
        assert prefixed_root_listing == root_listing

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), _RejectingProxyHandler)
        proxy_thread = threading.Thread(target=proxy.serve_forever)
        proxy_thread.start()
        _RejectingProxyHandler.attempted_targets.clear()
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        install_environment = environment.copy()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            install_environment[name] = proxy_url
        install_environment["NO_PROXY"] = "127.0.0.1,localhost,::1"
        install_environment["no_proxy"] = install_environment["NO_PROXY"]

        install_targets = (
            (index_url, "current"),
            (f"http://127.0.0.1:{server.server_port}/repo/current/simple", "prefix"),
            (
                f"http://127.0.0.1:{server.server_port}/{generation.as_posix()}/simple",
                "retained",
            ),
        )
        for install_index, environment_name in install_targets:
            consumer_path = tmp_path / environment_name
            subprocess.run(
                (
                    uv,
                    "venv",
                    "--no-config",
                    "--quiet",
                    "--python-preference=only-system",
                    f"--python={sys.executable}",
                    str(consumer_path),
                ),
                check=True,
                capture_output=True,
                text=True,
                env=environment,
                timeout=300,
            )
            consumer_python = consumer_path / "bin" / "python"
            subprocess.run(
                (
                    uv,
                    "pip",
                    "install",
                    "--no-config",
                    "--no-cache",
                    "--quiet",
                    f"--python={consumer_python}",
                    "--only-binary=:all:",
                    f"--default-index={install_index}",
                    "--index-strategy=first-index",
                    "loopback-fixture==1.0",
                ),
                check=True,
                capture_output=True,
                text=True,
                env=install_environment,
                timeout=300,
            )
            imported = subprocess.run(
                (str(consumer_python), "-I", "-c", "import loopback_fixture; print(loopback_fixture.value)"),
                check=True,
                capture_output=True,
                text=True,
                env=environment,
                timeout=300,
            )
            assert imported.stdout.strip() == "from the local index"
        expected_paths = (
            "/current/simple/",
            "/current/simple/loopback-fixture/",
            "/current/wheels/loopback_fixture-1.0-py3-none-any.whl",
            "/current/wheels/loopback_fixture-1.0-py3-none-any.whl.metadata",
            "/repo/current/wheels/loopback_fixture-1.0-py3-none-any.whl",
            "/repo/current/wheels/loopback_fixture-1.0-py3-none-any.whl.metadata",
            f"/{generation.as_posix()}/wheels/loopback_fixture-1.0-py3-none-any.whl",
            f"/{generation.as_posix()}/wheels/loopback_fixture-1.0-py3-none-any.whl.metadata",
        )
        for requested_path in expected_paths:
            assert requested_path in _RecordingIndexHandler.requested_paths

        wheel.unlink()

        assert _RejectingProxyHandler.attempted_targets == []
        assert main(publish_arguments) == 0
        assert (output / "current").readlink() == generation
        assert tuple(
            sorted(path.name for path in (output / "generations").iterdir())
        ) == initial_generations
        assert not wheel.exists()
    finally:
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()
        if proxy_thread is not None:
            proxy_thread.join()
        server.shutdown()
        server.server_close()
        server_thread.join()
