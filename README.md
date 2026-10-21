# Build wheels for a local Python index

`mypypi` resolves package pins, acquires or builds wheels, validates them, and publishes a static package index. It requires CPython 3.15 and `uv`.

## Install and configure

From the repository checkout, install the command with:

```sh
uv tool install --python 3.15 --from . mypypi
```

Ensure uv's tool executable directory is on `PATH`. Run `mypypi` from a directory containing `pyproject.toml` and `packages.toml`. Those are the default configuration and catalog paths; `.mypypi` is the default working state directory. Pass `--config`, `--catalog`, and `--state-dir` before the subcommand to use other paths. Use `mypypi <command> --help` for command options.

Add exact root pins to the catalog. Dependencies are resolved for the configured targets and retain their target applicability.

```sh
mypypi add "idna==3.10"
```

## Build wheels on native hosts

| Platform | Wheel OS floor | Native build host |
| --- | --- | --- |
| `linux-x86_64` | `manylinux_2_28` | Linux x86_64 |
| `linux-aarch64` | `manylinux_2_28` | Linux aarch64 |
| `macos-x86_64` | macOS 15.0 | macOS x86_64 |
| `macos-arm64` | macOS 15.0 | macOS arm64 |

Set the target list and Linux repair policy in `[tool.mypypi.targets]` in `pyproject.toml`. Catalog entries can apply to selected platforms with `platforms`; `exclude_platforms` requires a reason for each exception. Keep multiple package versions as separate `[[packages]]` entries.

```toml
[[packages]]
name = "example-dependency"
version = "2.0"
platforms = ["macos-arm64"]
```

If source compilation is needed, build on the target's native host. Validation also runs on a native host for each selected target. Linux native wheels must meet the configured manylinux policy; macOS wheels must retain the 15.0 deployment floor. `mypypi` does not launch or provision builder containers. Native repair needs `auditwheel` on Linux or `delocate` on macOS in the CLI's Python environment; Linux also needs `patchelf` and `readelf` on `PATH`. Add `--with auditwheel` or `--with delocate` to the install command for the matching host.

## Build, validate, and publish

Select one or more configured targets with `--platform`. Validation and publication require a wheel for every applicable catalog entry in the selected scope, or a reasoned exclusion. Stage the selected runtime dependency closure in the candidate index or publish its dependencies first.

```sh
mypypi plan --output ./wheel-index --platform macos-arm64
mypypi build --output ./wheel-index --platform macos-arm64
mypypi validate --output ./wheel-index --platform macos-arm64
mypypi publish --output ./wheel-index --platform macos-arm64
```

The publisher writes project pages under `current/simple/` and serves wheels and metadata from the same output directory. Its relative links work at an origin root or under a URL path prefix.

## Serve and install

For local use, serve the output directory on loopback:

```sh
python3 -m http.server --bind 127.0.0.1 --directory ./wheel-index 8000
```

Install a published pin into a CPython 3.15 environment:

```sh
uv venv --python 3.15 .consumer
uv pip install --python .consumer/bin/python --only-binary=:all: \
  --default-index http://127.0.0.1:8000/current/simple \
  --index-strategy first-index "idna==3.10"
```

The example server binds only to loopback and provides no access control. Use a separate access-controlled HTTPS service when serving the index to other machines.

## Security

Source builds, `custom_prebuild` commands, and runtime import validation execute third-party code on the host. Environment cleanup is not a sandbox. Build and validate only trusted packages, and keep credentials off those hosts. See the [Apache-2.0 license](LICENSE).
