# Security and build limits

`mypypi` creates a local static package index. It does not provide authentication, authorization, TLS termination, or remote publication. The documented HTTP server binds to loopback. Put a separate, access-controlled HTTPS service in front of the index if other machines need access.

## Build dependencies and source archives

`add` resolves exact package pins through uv against public PyPI. `build` uses public PyPI over HTTPS for build requirements by default. Use `--build-index` with `--strict-build-deps` to require a curated build index. Use `--build-constraints` to constrain build requirements and `--require-build-hashes` to require hashes where uv supports them.

`uv build` runs the upstream PEP 517 backend from a downloaded source archive. That backend can execute arbitrary code. `mypypi` does not sandbox it. Build only trusted source or run the command in a disposable, isolated builder with no sensitive credentials. Use a builder that matches the target platform when compiling native extensions.

Package-specific `custom_prebuild` commands also execute on the build host. Each command must write under the temporary `{prefix}` directory. The setting does not enforce that boundary. These commands must not install or remove host packages.

Runtime validation has a narrower source policy. It uses only the selected curated index and the staged wheel set. It rejects source distributions, public PyPI, and non-loopback HTTP indexes. A missing runtime dependency fails validation unless you stage the dependency wheel or publish it to the curated index.

## Platform and reproducibility limits

The supported targets are the four platforms listed in the README. macOS wheels target macOS 15.0 or newer. Validation runs only on a native matching host. The CLI also requires the current host to match a target when it builds an sdist. It has no container launcher or cross-compilation setting. Run it inside a matching builder when you use a container. `uv build` alone does not cross-compile extensions.

The configured GitHub Actions workflows run lint and tests on Ubuntu x86_64. They do not prove that wheel builds or validation work on the other targets. Use a native builder for each platform you release.

`mypypi` verifies downloaded wheel hashes and keeps published wheel filenames immutable. It does not guarantee bit-for-bit reproducible upstream builds. Build backends, toolchains, and source archives can change. Use a pinned source release, a controlled builder, build constraints, and hashes when your build requirements support them. The project lock file covers the CLI development environment, not every upstream package build.

The generated index is a static directory tree. It has no background service, remote deployment path, or automatic cleanup policy for published generations.
