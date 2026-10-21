from __future__ import annotations

import hashlib
import io
import zipfile
from base64 import urlsafe_b64encode
from pathlib import Path

import mypypi.cli as cli
from mypypi.cli import main
from mypypi.validate import _local_platform

HOST_PLATFORM = _local_platform()
assert HOST_PLATFORM is not None


def _write_wheel(path: Path, package: str = "candidate-fixture") -> None:
    dist_info = f"{package.replace('-', '_')}-1.0.dist-info"
    members = {
        f"{package.replace('-', '_')}.py": b"value = 1\n",
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.4\nName: {package}\nVersion: 1.0\n"
            "Requires-Python: >=3.15,<3.16\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n\n"
        ),
    }
    record = io.StringIO()
    for name, content in members.items():
        digest = urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        record.write(f"{name},sha256={digest},{len(content)}\n")
    record.write(f"{dist_info}/RECORD,,\n")
    members[f"{dist_info}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _publish_arguments(catalog: Path, state: Path, output: Path) -> list[str]:
    return [
        "--catalog",
        str(catalog),
        "--state-dir",
        str(state),
        "publish",
        "--output",
        str(output),
        "--platform",
        HOST_PLATFORM,
    ]


def test_publish_rejects_a_partial_catalog_without_changing_current(tmp_path, capsys):
    catalog = tmp_path / "packages.toml"
    catalog.write_text(
        '[[packages]]\nname = "candidate-fixture"\nversion = "1.0"\n'
        '[[packages]]\nname = "missing-fixture"\nversion = "1.0"\n',
        encoding="utf-8",
    )
    state = tmp_path / "state"
    wheel_dir = state / "wheels"
    wheel_dir.mkdir(parents=True)
    (wheel_dir / "candidate_fixture-1.0-py3-none-any.whl").write_bytes(b"not inspected")
    output = tmp_path / "index"

    status = main(_publish_arguments(catalog, state, output))

    assert status == 1
    assert "no candidate wheel" in capsys.readouterr().err
    assert not (output / "current").exists()


def test_validate_applies_selected_target_exclusions(tmp_path):
    catalog = tmp_path / "packages.toml"
    catalog.write_text(
        '[[packages]]\nname = "excluded-fixture"\nversion = "1.0"\n'
        f'exclude_platforms = {{ "{HOST_PLATFORM}" = "unsupported upstream binary" }}\n',
        encoding="utf-8",
    )
    status = main(
        [
            "--catalog",
            str(catalog),
            "--state-dir",
            str(tmp_path / "state"),
            "validate",
            "--output",
            str(tmp_path / "index"),
            "--platform",
            HOST_PLATFORM,
        ]
    )

    assert status == 0


def test_publish_rejects_candidate_bytes_changed_during_validation(
    tmp_path, monkeypatch, capsys
):
    catalog = tmp_path / "packages.toml"
    catalog.write_text(
        '[[packages]]\nname = "candidate-fixture"\nversion = "1.0"\n',
        encoding="utf-8",
    )
    state = tmp_path / "state"
    wheel_dir = state / "wheels"
    wheel_dir.mkdir(parents=True)
    staged_wheel = wheel_dir / "candidate_fixture-1.0-py3-none-any.whl"
    _write_wheel(staged_wheel)
    original_hash = hashlib.sha256(staged_wheel.read_bytes()).hexdigest()

    def change_snapshot(wheels, **kwargs):
        wheels[0].write_bytes(b"changed during validation")
        return ()

    monkeypatch.setattr(cli, "validate_wheels", change_snapshot)
    output = tmp_path / "index"

    status = main(_publish_arguments(catalog, state, output))

    assert status == 1
    assert "changed during validation" in capsys.readouterr().err
    assert hashlib.sha256(staged_wheel.read_bytes()).hexdigest() == original_hash
    assert not (output / "current").exists()
