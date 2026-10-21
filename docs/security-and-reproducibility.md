# Security and build limits

`mypypi` writes a static package index. It does not provide authentication, authorization, TLS termination, or remote publishing. The README's HTTP server binds to loopback. Put a separate access-controlled HTTPS service in front of the index when other machines need access.

## Code execution

`uv build` executes the upstream PEP 517 backend from a downloaded source archive. Runtime validation installs wheels and imports discovered modules. Both operations execute third-party code. Environment cleanup is not a sandbox. Run them only for trusted packages and keep credentials off the build and validation host.

Package-specific `custom_prebuild` commands also execute on the host. Relative script paths use the directory where you invoke `mypypi`. Commands receive a temporary `{prefix}` for generated headers and libraries, but the tool does not enforce that writes stay there. Do not use the setting to install or remove host packages.

## Package sources

`add` resolves exact pins against public PyPI. Wheel and source downloads require HTTPS, verify PyPI's SHA-256 digest, and reject HTTPS redirects that downgrade to HTTP or add URL credentials. A release that returns 404 uses bounded exponential retry for up to one hour. Each socket operation and subprocess has a timeout. Timeout or cancellation stops the subprocess group.

Source builds use public PyPI over HTTPS by default. `--strict-build-deps` requires a non-public HTTPS index, an absolute local `file://` index, or a loopback HTTP index. Runtime validation ignores ambient indexes and installs only binary wheels from the local candidate snapshot. It never falls back to public PyPI or builds runtime dependencies from source.

The command policy removes ambient uv and pip index, constraint, override, build-isolation, hash-verification, and insecure-host settings. It preserves proxy, certificate, interpreter-download, and uv cache settings. This protects command policy from user environment overrides; it does not isolate downloaded code.

## Target compatibility

The project supports CPython 3.15 only. Builds and imports use the selected CPython executable. Native validation checks the full running patch version against `Requires-Python` and reports its executable, ABI, and platform. Cross-target resolution uses CPython 3.15.0 as its conservative patch baseline and requires later validation on the target's native runner.

Linux targets require manylinux 2.28-compatible output. `auditwheel` must verify and repair native wheels. Build source on a matching Linux architecture with a manylinux 2.28 compatible userspace. A newer Linux host or matching CPU alone does not prove the policy. `mypypi` does not launch containers. Provision `auditwheel`, `patchelf`, and `readelf` in the external builder. On macOS, `delocate` repairs native wheels and the final wheel must retain the macOS 15.0 deployment floor.

GitHub Actions runs tests on Linux x86_64 and aarch64 and macOS x86_64 and arm64. Those jobs do not establish bit-for-bit reproducibility. The lock file covers the CLI environment, not every upstream build backend or compiler.

## Published state

Wheels are immutable by filename and SHA-256. Generations contain deterministic HTML and a manifest of wheel and metadata hashes. The publisher merges the current manifest while holding its process lock, then atomically promotes a complete generation. Old generations remain available at their generation URL. Place the output directory under any URL prefix; project pages link to the wheel directory through that same generation URL.

Historical yanked reasons persist when a catalog entry is absent or omits yanked policy. Set `yanked = false` explicitly to clear a prior yanked state. A different wheel's publication does not change it.
