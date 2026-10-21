from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mypypi.cli import main


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


def test_uv_installs_from_local_index(tmp_path, monkeypatch):
    uv = shutil.which("uv")
    assert uv is not None
    source = tmp_path / "source"
    (source / "src" / "loopback_fixture").mkdir(parents=True)
    (source / "src" / "loopback_fixture" / "__init__.py").write_text(
        'value = "from the local index"\n', encoding="utf-8"
    )
    project_lines = (
        '[build-system]',
        'requires = ["uv_build>=0.13.4,<0.14"]',
        'build-backend = "uv_build"',
        '',
        '[project]',
        'name = "loopback-fixture"',
        'version = "1.0"',
        'requires-python = ">=3.15,<3.16"',
    )
    (source / "pyproject.toml").write_text("\n".join(project_lines) + "\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    wheel_dir = state_dir / "wheels"
    wheel_dir.mkdir(parents=True)
    output = tmp_path / "index"
    output.mkdir()
    catalog = tmp_path / "packages.toml"
    catalog.write_text(
        '[[packages]]\nname = "loopback-fixture"\nversion = "1.0"\n',
        encoding="utf-8",
    )
    environment = _uv_environment(tmp_path)
    environment["UV_OFFLINE"] = "1"
    subprocess.run(
        (
            uv,
            "build",
            "--no-config",
            "--quiet",
            "--wheel",
            "--python=3.15",
            f"--out-dir={wheel_dir}",
            str(source),
        ),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    wheel = next(wheel_dir.glob("*.whl"))
    original_wheel_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(output))
    )
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    index_url = f"http://127.0.0.1:{server.server_port}/current/simple"
    try:
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        assert main(
            [
                "--catalog",
                str(catalog),
                "--state-dir",
                str(state_dir),
                "validate",
                "--index-url",
                index_url,
                "--platform",
                "macos-arm64",
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
            "--index-url",
            index_url,
            "--platform",
            "macos-arm64",
        ]
        assert main(publish_arguments) == 0
        generation = (output / "current").readlink()
        initial_generations = tuple(
            sorted(path.name for path in (output / "generations").iterdir())
        )
        assert hashlib.sha256(wheel.read_bytes()).hexdigest() == original_wheel_hash

        consumer = tmp_path / "consumer"
        subprocess.run(
            (
                uv,
                "venv",
                "--no-config",
                "--quiet",
                "--python-preference=only-system",
                "--python=3.15",
                str(consumer),
            ),
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        python = consumer / "bin" / "python"
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), _RejectingProxyHandler)
        proxy_thread = threading.Thread(target=proxy.serve_forever)
        proxy_thread.start()
        _RejectingProxyHandler.attempted_targets.clear()
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        install_environment = environment.copy()
        install_environment.pop("UV_OFFLINE", None)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            install_environment[name] = proxy_url
        install_environment["NO_PROXY"] = "127.0.0.1,localhost,::1"
        install_environment["no_proxy"] = install_environment["NO_PROXY"]
        try:
            subprocess.run(
                (
                    uv,
                    "pip",
                    "install",
                    "--no-config",
                    "--quiet",
                    f"--python={python}",
                    "--only-binary=:all:",
                    f"--default-index={index_url}",
                    "--index-strategy=first-index",
                    "loopback-fixture==1.0",
                ),
                check=True,
                capture_output=True,
                text=True,
                env=install_environment,
            )
        finally:
            proxy.shutdown()
            proxy.server_close()
            proxy_thread.join()
        assert _RejectingProxyHandler.attempted_targets == []
        imported = subprocess.run(
            (str(python), "-c", "import loopback_fixture; print(loopback_fixture.value)"),
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert imported.stdout.strip() == "from the local index"

        assert main(publish_arguments) == 0
        assert (output / "current").readlink() == generation
        assert tuple(
            sorted(path.name for path in (output / "generations").iterdir())
        ) == initial_generations
        assert hashlib.sha256(wheel.read_bytes()).hexdigest() == original_wheel_hash
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()
