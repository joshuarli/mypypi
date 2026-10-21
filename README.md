# Build wheels for a local Python index

`mypypi` plans, builds, validates, and publishes pinned package wheels to a local PEP 503 index. It has no registry server, account system, database, or remote publishing command. It supports CPython 3.15 only.

## Set up the project

Install [uv](https://docs.astral.sh/uv/) and CPython 3.15. From the repository root, sync the locked developer environment:

```sh
uv python install 3.15
uv sync --locked --python 3.15
uv run --locked --python 3.15 mypypi --help
```

`uv_build` builds the CLI. `uv` manages the developer environment, resolves package dependencies, builds upstream source archives, and creates isolated validation environments. `packaging` parses package names, versions, and wheel tags.

## Add package pins

`packages.toml` stores exact package pins and optional, reasoned exceptions. Add a package and its resolved dependencies for every configured platform with:

```sh
uv run --locked --python 3.15 mypypi --catalog packages.toml --state-dir .mypypi add "packaging==26.3"
```

The CLI writes resolved pins to `packages.toml`. You can also edit the file directly:

```toml
[[packages]]
name = "packaging"
version = "26.3"
```

Keep multiple versions as separate `[[packages]]` entries. `likely_binary_ignore` maps source archive paths to reasons. `custom_prebuild` maps commands containing `{prefix}` to reasons. Each command must write under that temporary prefix. The `validation` table supports `validate_extras`, `validate_incorrect_missing_deps`, and `validate_skip_imports`. Those keys map an extra, requirement, or import name to a reason. `exclude_platforms` maps a platform to a reason. `yanked_reason` records why a yanked release is allowed. The publisher preserves that reason in the local index. Every exception needs a non-empty reason. See [security and build limits](docs/security-and-reproducibility.md) before adding build commands.

`--catalog` and `--state-dir` are global options. Put them before the subcommand. Put command-specific options after the subcommand.

## Plan and build wheels

The platform list and Linux repair policy come from `[tool.mypypi.targets]` in `pyproject.toml`. The supported platforms are:

| Platform | Wheel OS floor | Native build host |
| --- | --- | --- |
| `linux-x86_64` | `manylinux_2_28` | Linux x86_64 |
| `linux-aarch64` | `manylinux_2_28` | Linux aarch64 |
| `macos-x86_64` | macOS 15.0 | macOS x86_64 |
| `macos-arm64` | macOS 15.0 | macOS arm64 |

`--platform` selects one configured target. Repeat it to select more than one. `plan` checks upstream metadata and local wheels. `build` downloads compatible wheels or builds from a source archive when needed.

```sh
uv run --locked --python 3.15 mypypi --catalog packages.toml --state-dir .mypypi plan --output ./wheel-index --platform macos-arm64
uv run --locked --python 3.15 mypypi --catalog packages.toml --state-dir .mypypi build --output ./wheel-index --platform macos-arm64
```

Build source archives on a matching native host. Linux wheels target `manylinux_2_28`. `pyproject.toml` records pinned manylinux image tags and digests, but `mypypi` does not launch those images. See [security and build limits](docs/security-and-reproducibility.md) before using a container builder.

Set `BUILD_INDEX` to the HTTPS URL for your curated build index before running this example:

```sh
uv run --locked --python 3.15 mypypi --catalog packages.toml --state-dir .mypypi build --output ./wheel-index --platform macos-arm64 --build-index "$BUILD_INDEX" --strict-build-deps --build-constraints build-constraints.txt --require-build-hashes
```

By default, build dependencies come from `https://pypi.org/simple`. `--strict-build-deps` rejects public PyPI hosts and requires a curated build index. `--build-constraints` passes a constraints file to `uv build`. `--require-build-hashes` asks uv to require hashes for build requirements. Runtime validation does not use this build index.

## Validate and publish locally

Start a local HTTP server bound to loopback. Keep it running while you validate and publish. The server exposes the `current` generation after the first publish.

```sh
mkdir -p ./wheel-index
python3.15 -m http.server --bind 127.0.0.1 --directory ./wheel-index 8000
```

In another terminal, validate the staged wheels against the curated runtime index and publish them to a local directory:

```sh
uv run --locked --python 3.15 mypypi --catalog packages.toml --state-dir .mypypi validate --index-url http://127.0.0.1:8000/current/simple --platform macos-arm64
uv run --locked --python 3.15 mypypi --catalog packages.toml --state-dir .mypypi publish --output ./wheel-index --index-url http://127.0.0.1:8000/current/simple --platform macos-arm64
```

Run validation on a native host that matches the selected platform. Validation uses only the supplied index and the staged wheel set. It installs wheels only, creates a fresh CPython 3.15 environment, and imports discovered modules. It never falls back to public PyPI or builds runtime dependencies from source. Stage the full dependency closure or publish dependencies to the curated index first.

Create a consumer environment and install from the local index:

```sh
uv venv --python 3.15 .consumer
uv pip install --python .consumer/bin/python --only-binary=:all: --default-index http://127.0.0.1:8000/current/simple --index-strategy first-index "packaging==26.3"
```

The publisher stores immutable wheels and atomically switches the relative `current` symlink after it writes a complete index generation. Identical republishing is a no-op. A filename collision with different bytes fails.

## Run checks

```sh
uv run --locked --python 3.15 ruff check src tests
uv run --locked --python 3.15 pytest -q tests
uv run --locked --python 3.15 pytest -q tests/test_ci_targets.py
```

The GitHub Actions workflows run lint and tests with read-only repository permissions on Ubuntu x86_64. They do not build wheels for every configured platform, publish packages, or upload artifacts. The target drift test checks the README platform list against `pyproject.toml`. The project includes the upstream Apache-2.0 license and copyright notice in [LICENSE](LICENSE).

Read [security and build limits](docs/security-and-reproducibility.md) before building third-party source archives or serving an index to other machines.
