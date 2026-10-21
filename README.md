# Build wheels for a local Python index

`mypypi` resolves, acquires, builds, validates, and publishes wheels to a static PEP 503 index. It supports CPython 3.15 on Linux x86_64 and aarch64, and macOS x86_64 and arm64.

## Set up the project

Install [uv](https://docs.astral.sh/uv/) and CPython 3.15. Run project commands from the repository root, or pass `--config` and `--catalog` with explicit paths.

```sh
uv python install 3.15
uv sync --locked --python 3.15
uv run --locked --python 3.15 mypypi --help
```

Use `uv sync --locked --python 3.15 --group repair-macos` when you build native macOS wheels. Use `--group repair-linux` on Linux. Linux repair also requires `patchelf` and `readelf` on `PATH`.

## Add and plan package pins

Add an exact root pin. `add` resolves dependencies for each configured target and records platform applicability on transitive pins.

```sh
uv run --locked --python 3.15 mypypi --config pyproject.toml --catalog packages.toml --state-dir .mypypi add "idna==3.10"
uv run --locked --python 3.15 mypypi --config pyproject.toml --catalog packages.toml --state-dir .mypypi plan --output ./wheel-index --platform macos-arm64
```

Keep multiple versions as separate `[[packages]]` entries. A transitive pin can apply to selected targets:

```toml
[[packages]]
name = "example-dependency"
version = "2.0"
platforms = ["macos-arm64"]
```

`exclude_platforms` maps exceptional platform names to reasons. The full target list and the Linux repair floor live in `[tool.mypypi.targets]` in `pyproject.toml`.

## Build wheels on native hosts

Run `build` on a host matching the selected platform when source compilation is needed. The command tries compatible upstream wheels in preference order, then tries each available source archive. It reports the filename and whether it came from an upstream wheel or a source build.

| Platform | Wheel OS floor | Native build host |
| --- | --- | --- |
| `linux-x86_64` | `manylinux_2_28` | Linux x86_64 |
| `linux-aarch64` | `manylinux_2_28` | Linux aarch64 |
| `macos-x86_64` | macOS 15.0 | macOS x86_64 |
| `macos-arm64` | macOS 15.0 | macOS arm64 |

Commands report the selected executable, full CPython patch version, ABI, and platform. Native builds and import validation use that interpreter and enforce its full patch version against `Requires-Python`. When resolving or checking a different target, the patch is unknown, so `mypypi` uses CPython 3.15.0 as the conservative baseline.

```sh
uv run --locked --python 3.15 mypypi --config pyproject.toml --catalog packages.toml --state-dir .mypypi build --output ./wheel-index --platform macos-arm64
```

Linux native builds require a matching Linux architecture host with a manylinux 2.28 compatible userspace. `auditwheel` checks and repairs the final policy. A newer Linux host does not establish that policy by itself. `mypypi` does not launch containers or select builder images. Prepare the matching builder with CPython 3.15, uv, `auditwheel`, `patchelf`, and `readelf` before building.

Build dependencies use public PyPI over HTTPS by default. Use `--build-index` with `--strict-build-deps` for a curated remote index. Strict builds also accept an absolute local `file://` index or loopback HTTP index. `--build-constraints` resolves its path from the directory where you invoke `mypypi`. Relative script paths in `custom_prebuild` use that same directory. Each command receives a temporary `{prefix}` path for generated headers and libraries.

## Validate and publish

Validation builds one candidate snapshot from retained published wheels and selected staged wheels. It applies package target applicability and exclusions, checks every selected catalog obligation, and resolves runtime dependencies only from that snapshot. It creates a fresh CPython 3.15 environment, installs wheels only, and runs imports from an isolated interpreter in a neutral directory. Validation runs on a native host for each selected target.

```sh
uv run --locked --python 3.15 mypypi --config pyproject.toml --catalog packages.toml --state-dir .mypypi validate --output ./wheel-index --platform macos-arm64
uv run --locked --python 3.15 mypypi --config pyproject.toml --catalog packages.toml --state-dir .mypypi publish --output ./wheel-index --platform macos-arm64
```

The first publish can start with an empty output directory. Stage the full selected runtime dependency closure or publish the dependencies first. A catalog entry with no wheel or reasoned exclusion fails validation and publication.

## Serve and install the index

The publisher writes an explicit project list at `current/simple/index.html`. It stores wheels under `wheels/` and promotes complete immutable generations through the relative `current` symlink. Generated wheel links work at the origin root, below a path prefix, and through retained generation URLs.

```sh
uv run --locked --python 3.15 python -m http.server --bind 127.0.0.1 --directory ./wheel-index 8000
```

In another terminal, install a published pin:

```sh
uv venv --python 3.15 .consumer
uv pip install --python .consumer/bin/python --only-binary=:all: --default-index http://127.0.0.1:8000/current/simple --index-strategy first-index "idna==3.10"
```

Identical publication is a no-op. Different bytes under a published wheel filename are a conflict. An unrelated publication preserves historical yanked reasons. Set `yanked = false` on a current catalog entry to clear that wheel's yanked state explicitly.

## Run checks

Unit and loopback fixture tests do not need public PyPI. The marked resolver smoke test intentionally resolves an extra against PyPI.

```sh
uv run --locked --python 3.15 ruff check src tests
uv run --locked --python 3.15 pytest -q -m "not network" tests
uv run --locked --python 3.15 pytest -q -m network tests/integration/test_uv_resolution.py
```

The workflows run lint and tests on Linux x86_64 and aarch64 and macOS x86_64 and arm64. They use read-only repository permissions and do not publish packages. See [security and build limits](docs/security-and-reproducibility.md) before building untrusted source. The project includes the upstream Apache-2.0 license and attribution.
