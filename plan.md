# Build a minimal, uv-native Python wheel factory

Status: complete with explicit review-tool and platform limitations.

## Goal

Build a small Python CLI in this repository that creates, validates, and publishes CPython 3.15 wheels to a static local package index. Reuse the valuable algorithms in the upstream ../pypi repository while removing Sentry services, global package-manager side effects, and unnecessary layers.

The tool must run on macOS and Linux without Google Cloud, a database, or a persistent application server. It must build on a host that matches each native target. It must never publish a wheel before validation succeeds.

## Deliverables

1. Ship the mypypi CLI with add, plan, build, validate, and publish commands.
2. Read a small, editable packages.toml catalog with exact package pins and explicit build and validation exceptions.
3. Plan and acquire compatible upstream wheels, verify their hashes and architectures, and build from sdists with each upstream PEP 517 backend when needed.
4. Validate every new wheel in a fresh CPython 3.15 environment using only the curated index and the current uncommitted wheel set.
5. Publish immutable wheels and a deterministic PEP 503 HTML index to a local filesystem with atomic generation promotion and recovery.
6. Preserve incremental builds, dependency expansion through uv, and useful failure reports.
7. Add focused tests, a local HTTP end-to-end test, read-only GitHub Actions workflows, compact docs, and the upstream Apache-2.0 license.
8. Remove copied Sentry behavior, dependencies, tests, and docs that have no live consumer in this tool.

## Charter

- Work only in this repository. Read ../pypi at the recorded reference revision. Do not modify that checkout.
- This plan does not authorize commits, pushes, or remote artifact publication. Do not push. Write source, tests, docs, and workflows only as requested here.
- Read public PyPI metadata and artifacts when a command needs them. Local end-to-end tests may bind only to loopback.
- Preserve existing staged and unstaged user changes. Do not reset, clean, or overwrite user-owned files.
- Before the first campaign scratch write, confirm .work/ is ignored through .git/info/exclude. Keep worktree copies under .work/wt/<lane>, other scratch under .work/tmp, and set TMPDIR=.work/tmp for commands that spill large files.
- The campaign starts with one integrator because shared types and configuration need one owner. After those land, run one writer per disjoint ready workstream, up to the capacity reported by perf-gate-ready -s. Measure lane peak memory once and set PERF_GATE_LANE_GIB from that value. Recheck capacity before each launch round. If the host allows only one lane, record why and work serially.
- Local commits are not authorized by this plan. Use disjoint patch copies under .work/wt/<lane> for parallel writers. Each lane owns only the files named in its item, writes only in its copy, and hands the copy back for review. The integrator applies disjoint owned files to the integration checkout and verifies them there. If a change needs a shared file, assign it to the integrator or sequence it after the lane. Do not create Git worktrees when the repository has no commit to check out. Do not commit or push.
- Keep the integration checkout out of parallel tests. Run verifiers in separate copies under .work/wt/verify-<name>, with distinct basetemp directories, when their inputs are disjoint. Never collect tests twice from the same tree.
- When a lane stops, resume it with the ruling or start a lane on the remaining disjoint work in the same turn. Integrate finished lanes while the next ready work starts. Do not wait for the whole wave before integrating.
- Add no code comments. Name the constraint in code or a test. Preserve a comment only for an external protocol or platform quirk that cannot be represented elsewhere.
- For each failure, reproduce the smallest input, state one hypothesis, and run the cheapest observation that could disprove it. After three refuted hypotheses, recheck the executable, version, branch, configuration, cache, and code path before trying another fix. Keep the cause-specific regression test. After three failed fix attempts, record the finding and continue with work that does not depend on it.
- Use no formatter. Run focused tests by name while implementing. Run the full suite at the parity gate and close. Do not widen checks without naming the integration risk.
- Before timing, require perf-gate-ready to exit 0. Run it again with -1 immediately after each timing. If the host stays busy, record “perf gate deferred: host busy”; keep functional work moving, but do not call the campaign complete.
- Measure and log build time and the installed CLI wheel size before and after each production dependency. No dependency is approved on its name alone. Check its maintenance, transitive dependencies, license, and whether the standard library or a few direct lines suffice. No performance threshold is defined yet, so report these measurements without gating feature work.
- Every material contract change appears in the decision log and final report. Every commit, if separately authorized later, must use a Conventional Commit with its workstream as scope. The item ID belongs in the body.

## Decisions

### Owner-decided

- 2026-10-21: Support CPython 3.15 only. Reject other Python minor versions in project metadata, configuration, tests, and docs. Do not add a Python-version matrix. The package and build target is >=3.15,<3.16.

### Prescribed

- Name the distribution, import package, and console command mypypi, based on this repository name.
- Keep pyproject.toml as the sole target authority. Set project.requires-python to >=3.15,<3.16; derive the build and validation interpreter from that value. Put the supported platform list and Linux repair policy under [tool.mypypi.targets]. Do not repeat Python versions in packages.toml, code constants, or workflow matrices.
- Support linux-x86_64, linux-aarch64, macos-x86_64, and macos-arm64. A native build runs on a matching host, container, or named cross-compilation toolchain. Never infer that uv build cross-compiles an extension.
- Reuse the upstream manylinux compatibility floor if its builder metadata establishes it. If it cannot be established, use manylinux_2_28 for the example configuration and log that choice. Keep the Linux policy configurable and pin the selected builder image by immutable digest. Keep the human-readable image tag beside the digest.
- Use only packaging at runtime for names, versions, specifiers, and wheel tags. Use uv_build for this pure-Python CLI. Use uv for developer environments, dependency resolution, upstream sdist builds, and isolated validation. Use auditwheel on Linux and delocate on macOS only where repair is required. Do not add pip, pip-tools, Poetry, Hatch, setuptools, dumb-pypi, or a TOML comment-preserving dependency.
- Let tomllib parse packages.toml. Make add append validated entries without reformatting existing text, so human comments survive. If preserving comments requires a TOML-writing dependency after a real fixture proves append-only writing insufficient, the integrator must first check its maintenance, dependency weight, and license, then measure sync time and wheel size.
- Retain the narrowly scoped likely_binary_ignore and custom_prebuild exceptions. A custom prebuild may write only under its temporary prefix; it must not install or remove host packages. Reuse uv's cache for verified downloads and build intermediates when its cache key covers the source, target, build settings, and toolchain.
- Retain exact pins and multiple historical versions for the same distribution. Keep package build and validation exceptions in packages.toml. Reject unknown keys. Do not support python_versions: the only supported interpreter is 3.15, and package Requires-Python metadata is authoritative. A required package that excludes 3.15 fails with a useful error.
- Reject yanked upstream files by default. Permit one only through an explicit package entry with a nonempty reason. Preserve yanked metadata in the local index when configured.
- Publish only to a local filesystem. Omit SSH and rsync recipes, remote deployment commands, and any credential flow because no remote write authority was requested.
- Allow build dependencies from public PyPI under a documented bootstrap policy. Support build constraints and hashes when uv can enforce them. --strict-build-deps requires build dependencies from the curated index. Runtime validation always uses only the curated index and the just-built wheel set; it never falls back to public PyPI and never builds sdists.
- Use HTTPS for public PyPI access. Retain bounded exponential retry for a release that returns 404 while it is appearing on PyPI, with the upstream one-hour deadline. Do not retry invalid metadata, integrity failures, incompatibility, or user cancellation.
- Keep validation exceptions explicit: validate_extras, validate_incorrect_missing_deps, and validate_skip_imports. Require an explanation for each exception. Skip only the named package, target, or import.
- Store reusable source hashes, target details, build settings, and toolchain identity only in a cache key that the cache reader uses. Do not write a separate provenance log. Keep manifest.json because the publisher needs a deterministic inventory and artifact hashes.
- Generate the HTML Simple API first. Do not add JSON negotiation. Use normalized distribution names, sorted links, relative URLs, SHA-256 wheel links, data-requires-python, PEP 658/714 metadata sidecars and hashes, yanked metadata where configured, and escaped HTML.
- Keep generated artifact metadata deterministic. Do not include the upstream upload_timestamp or uploaded_by fields; they depend on the clock and Git checkout rather than wheel contents.
- Store immutable generations under the output directory and atomically replace a relative current symlink only after wheels, metadata, and indexes are complete. Stage on the destination filesystem. A failed or interrupted publish leaves the old generation visible. Use a nonblocking process lock; a concurrent publisher exits with a conflict error. On startup, remove only this tool's abandoned staging directories while holding the lock. Reject symlinked output roots and archive members that escape staging.
- Treat different bytes under one wheel filename as a hard collision. Identical bytes are a no-op. A repeated publish with no changes creates no generation and changes no wheel bytes.
- Keep error context as structured values until the CLI boundary. Report package, version, Python 3.15, platform, and failure stage. Required build or validation failures exit nonzero. Do not let a warning turn a required failure into success.
- Add no compatibility layer for Sentry. Do not copy its historical package catalog. Keep the upstream checkout read-only, and stop referencing it after parity work ends.
- Keep the 500–1,500-line core goal as a reportable aspiration. Do not delete correctness checks to meet a line count.

### Delegated within bounds

- Each lane may choose module internals and test fixtures inside its owned files. Keep parsing and decision logic pure where useful, and keep process, network, filesystem, clock, and environment effects at narrow boundaries.
- The catalog lane may choose the compact TOML table shape, but it must satisfy the rules above and preserve existing comments on add.
- The build lane may choose the native binary inspection mechanism. It must inspect shared libraries and executable artifacts, verify actual architectures, and reject archive traversal before extraction.
- The integrator selects the pinned manylinux image and records its configured policy. Prefer the upstream floor; otherwise apply the manylinux_2_28 tie-break above.
- If platform evidence or a dependency is unavailable, record the exact missing fact and keep independent work moving. Do not ask the owner to resolve a technical blocker.

## Baseline

At preflight on 2026-10-21, the destination had no commit, source package, test suite, project configuration, or build loop. plan.md was the only project file. There is no existing target command to time.

The revision originally recorded for getsentry/pypi, 5771ec5d8de64a0b332b48f2ab3fdc7adffb053b, is not present in the local checkout or public origin. The clean ../pypi checkout is at 5771ec5a00d3b54425c75bc455fc4a4f93d9285f. That origin commit was fetched into .work/tmp/upstream-pypi and is the verified reference. Its build.py, validate.py, and docker/install-pythons list CPython 3.11 through 3.14. Its tests encode those versions. It has no Python 3.15 behavior to compare. validate.py downgrades Python 3.14 validation failures to warnings. The new 3.15 validator must fail closed.

At preflight, system Python 3.15 was available but did not have packaging installed, so a direct import of upstream build.py stopped at ModuleNotFoundError: packaging. During P0, the project environment resolved packaging 26.3 and imported the pinned upstream build.py successfully. A synthetic sdist containing a .pyx file exercised the real _likely_binary implementation and returned its compiled-source finding. The source has no Python 3.15 build behavior to compare.

| Command | P0 timing | Next measurement |
| --- | --- | --- |
| uv sync --locked --python 3.15 | 0.49 s wall, 0.41 s CPU for the first dev sync; 0.04 s wall for a cached sync. packaging was already pulled in by pytest. | P3 on the final locked environment |
| Focused P0 pytest | 0.58 s wall, 0.54 s CPU; pytest reported 0.07 s for 18 tests. | P3 on the representative focused loop |
| mypypi --help | 0.29 s wall, 0.13 s CPU. | P3 on the representative CLI path |
| Full pytest suite | No suite existed before P0. | P1 parity gate |

P0 ran on macOS arm64 with CPython 3.15.0 and uv 0.13.4. perf-gate-ready and the immediate perf-gate-ready -1 check both exited 0 for every timing. A focused unit test should finish within 10 seconds. If the focused loop exceeds 30 seconds, fix the loop before feature lanes start. Until parity is complete, record performance measurements but do not gate feature work.

At the final optimization phase, compare the representative post-parity workflow with its P0 baseline on the same host, inputs, Python, uv, and cache state. Use at least five interleaved runs and report the median and range. Confirm each run produced the same plan or wheel set. Fix a measured regression greater than 2x or a focused check above 30 seconds. Do not claim a speedup without naming the measured work and output.

## Sequence

### P0: Ground the project and loop

1. **P0-1: Pin and map the reference.** Size S. No prerequisites. Owner: integrator. Files: plan.md only. Confirm the reference commit, read its build, validation, dependency expansion, index, tests, configuration, docs, license, and workflows. If ../pypi is absent or at a different commit, prepare the pinned read-only checkout under .work/tmp/upstream-pypi. Do not change ../pypi. Record existing failures as source behavior, not destination failures. Proof: the Reference section names the exact commit and maps each behavior to source files.
2. **P0-2: Create the 3.15 project shell and narrow loop.** Size M. Requires P0-1. Owner: integrator. Files: pyproject.toml, uv.lock, src/mypypi/__init__.py, src/mypypi/__main__.py, src/mypypi/cli.py, src/mypypi/config.py, src/mypypi/models.py, packages.toml, tests/test_cli.py, tests/test_models.py, tests/test_targets.py. If uv cannot find CPython 3.15, provision it with uv python install 3.15. Set requires-python = ">=3.15,<3.16", add uv_build, packaging, pytest, and Ruff only after the dependency checks above. Put the four platform targets in one authoritative config. Define only the shared types needed for package identity, target, wheel identity, plan outcome, validated artifact, and CLI errors. Implement help output for all five commands. Generate uv.lock with uv lock --python 3.15, then verify with uv sync --locked --python 3.15; uv run --locked --python 3.15 pytest -q tests/test_targets.py tests/test_models.py tests/test_cli.py; and uv run --locked --python 3.15 mypypi --help. Tests reject 3.14 and 3.16 metadata and accept 3.15.

After P0, launch the four disjoint feature workstreams together when capacity allows. Use one lane per ready stream. The expected lane floor is four while all four remain ready; the ceiling is four, bounded by perf-gate-ready -s. If capacity is lower, run the safe width and launch replacements as soon as a lane finishes.

### P1: Reach feature and reference-test parity

3. **P1-A: Catalog, dependency expansion, and planning.** Size L. Requires P0-2. Owns packages.toml, src/mypypi/catalog.py, src/mypypi/add.py, src/mypypi/planner.py, tests/test_catalog.py, tests/test_add.py, and tests/test_planner.py. Do not edit pyproject.toml, uv.lock, shared models, or CLI dispatch. Parse and validate exact package keys, canonical names, multiple historical versions, optional build and validation settings, platform exclusions with reasons, comments, and unknown-key failures. Use uv pip compile for dependency closure, with Python 3.15 and each configured platform so environment markers are accounted for. Detect conflicts explicitly. Plan already-published compatible wheels, compatible upstream downloads, sdist builds, explicit unsupported platforms, pending validation, and collisions. Proof: focused tests cover each status, dependency markers, pinned input, conflicts, comments surviving add, and an unchanged catalog producing an empty plan.
4. **P1-B: Acquire and build wheels.** Size L. Requires P0-2. Owns src/mypypi/acquire.py, src/mypypi/build.py, src/mypypi/platforms.py, tests/test_acquire.py, and tests/test_build.py. Do not edit shared configuration, models, CLI, or publisher files. Reuse compatible upstream wheels in tag preference order. Preserve pure-Python and abi3 reuse for CPython 3.15. Check package identity, release Requires-Python, file hashes, yanked status, and native architectures inside the wheel. If no acceptable wheel exists, require an sdist, verify its hash, and let uv build --wheel invoke the upstream PEP 517 backend. Detect sdists likely to contain compiled code and fail if the produced wheel lacks the expected binary. Repair Linux wheels with auditwheel and macOS wheels with delocate. Do not install or remove global apt or Homebrew packages. Require a prepared build environment or the bounded package-specific prebuild setting. Proof: focused synthetic tests cover tag preference, abi3, pure wheels, hash mismatch, wrong architecture, missing sdist, invalid metadata, wrong wheel name or version, missing compiled output, and repair invocation. Run one tiny real compiled-wheel fixture on each available supported builder.
5. **P1-C: Validate wheels.** Size L. Requires P0-2. Owns src/mypypi/validate.py, tests/test_validate.py, and tests/fixtures/wheels/. Do not edit acquisition, catalog, shared models, or publisher code. For each newly acquired or built wheel, check wheel structure, filename tags, package name and version, Requires-Python, and target architecture. Create a fresh CPython 3.15 uv venv; install only from the curated index and the just-built wheel set; use only binaries; confirm required dependencies; import discovered top-level modules except explicit skips. Apply explicit extras and missing-dependency exceptions. Return a structured failure with package, version, Python, platform, and stage. Proof: tests cover strict no-public-PyPI resolution, no sdist installation, missing dependency, import failure, each validation exception, and nonzero CLI status for required failures.
6. **P1-D: Store artifacts and publish the static index.** Size L. Requires P0-2. Owns src/mypypi/store.py, src/mypypi/index.py, tests/test_store.py, and tests/test_index.py. Do not edit CLI, shared models, catalog, or builder files. Accept only validated artifacts. Write immutable wheels, metadata sidecars, manifest, and normalized PEP 503 pages into a staged generation. Sort output and escape HTML. Include relative links with SHA-256, data-requires-python, PEP 658/714 sidecar links and hashes, and configured yanked metadata. Promote only by an atomic relative-symlink replacement. Recover abandoned staging data under the process lock. Proof: tests cover deterministic output, metadata hashes, identical-file no-op, conflicting wheel filenames, concurrent-publisher conflict, output-root symlinks, archive path traversal, interruption before promotion, previous-generation recovery, and unchanged repeated publication.
7. **P1-E: Integrate CLI and complete one local workflow.** Size L. Requires P1-A through P1-D. Owner: integrator. Files: src/mypypi/cli.py, tests/integration/test_local_index.py, and only shared-file edits assigned by the integrator. Implement add, plan, build, validate, and publish --output. Add repeatable --platform selection without changing the manifest. Keep build dependency bootstrap separate from runtime resolution. Run an end-to-end test that builds a real fixture wheel with uv build, publishes a local index, serves it over loopback, creates a fresh uv environment, installs only from that index, imports the fixture, and publishes again without changing wheel bytes or creating a generation. Run with network disabled after the test environment is provisioned. Proof: uv run --locked --python 3.15 pytest -q tests/integration/test_local_index.py::test_uv_installs_from_local_index.

### P2: Close edge cases, docs, and workflows

8. **P2-A: Finish the parity register and hardening.** Size L. Requires P1-E. Owners: existing feature lane for each owned module; integrator coordinates. Files: existing test and module files only. Compare the new implementation with the pinned upstream behavior on shared fixtures. Keep a named test for each retained behavior. Record each intentional difference once. Cover malformed input, invalid yanked metadata, interrupted downloads, failed build cleanup, cancellation, partial staging, repeated publication, and useful CLI exit codes. Use the cause-first failure rule. Do not retry failures to green. Proof: uv run --locked --python 3.15 pytest -q tests; run focused regressions by name first. This is the first full-suite gate because every listed feature and parity test must now exist.
9. **P2-B: Write user docs and read-only CI.** Size M. Requires P1-E. Docs and workflow writers own README.md, docs/, .github/workflows/, LICENSE, and tests/test_ci_targets.py. Do not change application code or shared configuration. Document purpose and non-goals, uv installation, the catalog, add, plan, build, validate, local publishing and serving, uv consumption, tests, build dependency bootstrap, native build exceptions, supported platforms, Python 3.15-only support, and security and reproducibility limits. Explain uv_build, uv build, and uv pip. State that building third-party sdists executes upstream code and needs a trusted or sandboxed builder. Retain the Apache-2.0 license and notices. Add minimal lint and test workflows. Make the platform matrix derive from the target configuration or test that it matches. Give workflows read-only permissions. Never publish to a production index or upload persistent artifacts. Proof: README commands pass locally; the target-drift test passes; validate workflow syntax with actionlint if installed, otherwise inspect the workflow and run the local commands it names.
10. **P2-C: Run target-specific platform checks.** Size M. Requires P1-B, P1-C, and P2-B. Integrator owns .work/wt/verify-* copies and platform result collection. Run tests on matching macOS arm64, macOS x86_64, Linux x86_64, and Linux aarch64 builders where available. At least one real compiled wheel must pass build, repair, validation, and install on each available builder. Do not emulate a native architecture and report it as native evidence. Record any unavailable builder as an unverified platform limitation. Proof: the relevant named platform tests and the local read-only workflow checks pass.

### P3: Measure and optimize

11. **P3-A: Measure representative work and dependency cost.** Size M. Requires P2-A through P2-C. Owner: integrator. Files: no new benchmark framework; retain only test fixtures needed to reproduce a useful measurement. Measure mypypi plan on the example catalog and a cached build plan on a small synthetic catalog. Compare like-for-like outputs and verify useful work completed. Use five interleaved runs, report median and range, and check memory and installed wheel size. Optimize only a measured regression over 2x or a focused loop over 30 seconds. Do not add caching, parallelism, or dependencies without evidence that they improve a named user workflow.
### P4: Close

12. **P4-1: Remove dead paths and report the core size.** Size M. Requires P3-A. Owners: original module owners; integrator removes shared references. Search callers, tests, workflows, config, and docs before deleting each copied Sentry path. Remove GCS, Sentry hostnames and names, packages.json remote acquisition, global apt/Homebrew mutation, obsolete upgrade and formatter commands, dead tests, and compatibility shims from this repository. Do not delete or edit ../pypi. Keep the Apache license. Report core source lines against the 500–1,500 aspiration; correctness takes priority. Proof: rg finds no live references to removed infrastructure, and the focused suite passes.
13. **P4-2: Review and hand off.** Size M. Requires all prior items. Owner: integrator. Files: plan.md decision and campaign logs; TODO.md or docs/rfc/ only if the review produces findings to retain. Run ultrareview over this repository, fix its P1 and P2 findings, and keep any remaining accepted findings in TODO.md using the todo skill. Then run focused regressions, the full suite once more, the local HTTP end-to-end test, the lint command, and README examples. Review changes and existing user staging. Do not delete user files or scratch data without checking their contents. Remove only recorded campaign copies after their changes are integrated and verified. Append one short line to the campaign log. Report final layout, reused and changed upstream algorithms, dependencies and infrastructure removed, uv's role, commands and results, untested platforms, and actual limitations. Proof: all required local commands pass, every deliverable has a named proof, all P1/P2 review findings are resolved, and no work remains outside an explicit platform limitation.

## Acceptance

| Deliverable | Required proof |
| --- | --- |
| Python support | Project metadata, CLI, dependency resolution, wheel tags, validation, and docs accept CPython 3.15 only. Tests reject 3.14 and 3.16. |
| Catalog | uv run --locked --python 3.15 pytest -q tests/test_catalog.py covers canonicalization, exact pins, multiple versions, optional build and validation exceptions, platform exclusions, comments, unknown keys, and malformed entries. |
| Dependency expansion | uv run --locked --python 3.15 pytest -q tests/test_add.py covers uv resolution, dependency markers for every configured platform, conflicts, already-present entries, and comments preserved after adding packages. |
| Incremental plan | uv run --locked --python 3.15 pytest -q tests/test_planner.py distinguishes compatible published artifacts, upstream wheels, sdists, excluded platforms with reasons, pending validation, and conflicts. An unchanged catalog produces an empty plan. |
| Acquisition and build | uv run --locked --python 3.15 pytest -q tests/test_acquire.py tests/test_build.py covers compatibility ranking, pure and abi3 reuse, SHA-256 mismatch, yanked files, wrong architectures, source fallback, absent sdists, wrong package metadata, suspiciously pure output, and repair. |
| Validation | uv run --locked --python 3.15 pytest -q tests/test_validate.py covers wheel structure, tags, Requires-Python, isolated Python 3.15 installs, no public fallback, no source builds for runtime dependencies, required dependencies, import discovery, explicit exceptions, failures, and nonzero status. |
| Static index | uv run --locked --python 3.15 pytest -q tests/test_index.py covers normalized names, escaped deterministic HTML, relative URLs, wheel hashes, data-requires-python, PEP 658/714 sidecars and hashes, and yanked metadata. |
| Immutable publication | uv run --locked --python 3.15 pytest -q tests/test_store.py covers same-name same-bytes no-op, same-name different-bytes conflict, repeated idempotent publish, atomic generation switch, staging recovery, process interruption before and after promotion, and concurrent publishers. Restart leaves either the old complete generation or the new complete generation visible. |
| Local installation | tests/integration/test_local_index.py::test_uv_installs_from_local_index builds a real wheel with uv, serves the static index on loopback, installs only from that index into a fresh environment, imports the package, then republishes without changing existing wheel bytes. |
| Failures and reports | CLI tests cover missing sources, invalid config, integrity failures, build failure, validation failure, publication conflict, nonzero exit, and diagnostics naming package, version, Python, platform, and stage. |
| CI and docs | uv run --locked --python 3.15 pytest -q tests/test_ci_targets.py checks workflow platforms against the configured targets. README commands pass locally. Pull-request workflows do not publish or upload artifacts and have read-only permissions. |
| Completion | Full tests, lint, README examples, local end-to-end test, and available platform checks pass. The final report names any platform that could not be tested. |

Use synthetic package fixtures for ordinary tests. Keep the unit suite independent of external network access. The end-to-end test uses loopback only and runs offline after setup. Test real compiled-wheel behavior on each supported builder that is available.

## Execution boundary

This plan allows changes to source, tests, documentation, project configuration, and the requested read-only workflows in this repository after the owner launches the campaign. It does not authorize commits, pushes, changes to ../pypi, global package installation, remote publication, or changes outside this repository. Stop only the item that needs an effect outside this boundary; continue independent work.

Public API and persisted-format changes in scope are mypypi's five CLI commands, packages.toml, the 3.15-only Requires-Python declaration, and the immutable generation layout with a relative current symlink. List any further contract changes in the decision log and final report.

## Non-goals

- Google Cloud, Sentry services or branding, remote deployment, credentials, or production publication.
- A registry daemon, database, administration interface, accounts, authentication, or a general artifact registry.
- A custom dependency resolver, replacement PEP 517 backends, distributed caching, or a build scheduler.
- A full historical Sentry catalog.
- Automatic changes to global apt, Homebrew, or other host packages.
- Docker as a requirement for the pure-Python local path.
- Windows.
- Python versions other than CPython 3.15.
- A JSON Simple API before the HTML API is complete and there is a tested consumer need.
- An artificial source line limit.

## Reference

### Upstream code map

The behavior reference is getsentry/pypi at commit 5771ec5a00d3b54425c75bc455fc4a4f93d9285f, checked out under .work/tmp/upstream-pypi. The separate ../pypi checkout remains clean at the same commit.

| Source | Behavior to inspect and compare |
| --- | --- |
| .work/tmp/upstream-pypi/build.py | Catalog parsing, release metadata, Python and platform tags, upstream file selection and hashes, architecture checks, sdist fallback, binary-source heuristic, platform repair, incremental reuse, retries, and the Python 3.14 warning-only branch. |
| .work/tmp/upstream-pypi/validate.py | Wheel tag selection, Requires-Python, top-level import discovery, fresh uv environments, index restrictions, validation exceptions, and failure reporting. |
| .work/tmp/upstream-pypi/add_pkg.py | uv dependency closure resolution and catalog updates. |
| .work/tmp/upstream-pypi/make_index.py | Wheel metadata and hashes, sidecars, existing artifact lookup, and the current dumb-pypi output contract. |
| .work/tmp/upstream-pypi/tests/ | Existing behavioral cases for build selection, package configuration, imports, metadata, add behavior, and index output. Use these as evidence; do not copy test-only APIs or old Python-version expectations. |
| .work/tmp/upstream-pypi/packages.ini | A large Sentry package catalog. Do not copy it. |
| .work/tmp/upstream-pypi/docs/ | Current build and manifest behavior, including global package-manager setup and remote publication. |
| .work/tmp/upstream-pypi/.github/workflows/build.yml | Linux and macOS builder shape, current GitHub Actions deployment job, and permissions to remove. |
| .work/tmp/upstream-pypi/LICENSE | Apache-2.0 license and notices to retain. |

The upstream tests cover many algorithmic cases, but they do not define Python 3.15 behavior. For shared behavior, run the source and new implementation on the same synthetic inputs and compare results, errors, and side effects. For 3.15 tags and builds, use explicit new tests because the source has no 3.15 target. Record intentional differences once. Do not copy the source's mutable image choice, global package installation, or warning-only 3.14 behavior.

### Project shape

Use this as a starting point, not a demand for a module per noun: src/mypypi/ contains __main__.py, cli.py, config.py, models.py, catalog.py, add.py, planner.py, acquire.py, build.py, platforms.py, validate.py, store.py, and index.py. Add a module only when it has one cohesive owner. The repository also contains tests/, docs/, packages.toml, pyproject.toml, uv.lock, README.md, and LICENSE.

Keep three environments separate: the CLI development environment, the isolated upstream build environment, and the isolated wheel validation environment. uv_build packages this CLI only. uv build invokes the upstream project's declared PEP 517 backend. uv pip resolves dependencies and installs wheels without requiring pip.

## Decision log

Append one short dated line for each judgment that changes a contract or settles an implementation detail. Include the call and the reason.

2026-10-21: Re-pinned the parity reference to 5771ec5a00d3b54425c75bc455fc4a4f93d9285f because the previously recorded revision is absent from the local object database and public origin; verified the available commit in an isolated scratch checkout without changing ../pypi.
2026-10-21: Use packaging>=26.3 as the sole runtime dependency; PyPI metadata lists no runtime dependencies, and the installed wheel is 126.9 KiB. Use uv_build>=0.13.4,<0.14 as the pure-Python build backend, pytest>=9.1.1 for tests, and ruff>=0.17.1 for linting. The backend and Ruff add no transitive packages. The no-dev sync took 0.21 s wall before and after packaging; the wheel build took 0.20 s wall in both cases, while wheel size grew from 5,133 to 5,149 bytes. The packaging install occupies 512 KiB on this filesystem.
2026-10-21: Set macOS wheel deployment to 15.0 because that is the compatibility floor used for target tags; source builds must set the same floor or newer hosts emit wheels the validator rejects. The supported macOS wheel contract is 15.0 or newer, and the docs drift test checks both architectures against the single policy constant.
2026-10-21: Set Ruff's line length to 120 to keep project and test code readable without introducing unrelated reflow; use Python 3.15 lint rules.
2026-10-21: Keep the staging owner marker through generation promotion and remove it afterward so crashes on either side of the atomic rename remain recoverable.
2026-10-21: Remove the unused `native_architectures` helper and `ErrorKind.CANCELLED`; interruption already maps directly to CLI status 130, so neither internal path had a caller.

## Campaign log

Append one short line at the end of each phase. Include the checks run and any parked platform item.

2026-10-21 P0: pinned the available upstream revision in .work/tmp without changing ../pypi; created the CPython 3.15 project shell and locked environment; 18 focused tests, Ruff, CLI help, and locked sync pass. The real upstream binary-source heuristic was exercised. Captured sync, test, CLI, dependency, and wheel-size baselines; no platform item parked.
2026-10-21 P1: integrated catalog, planning, acquisition/build, validation, immutable publication, and CLI lanes; the loopback end-to-end install and repeated publish path pass.
2026-10-21 P2: the 111-test suite, Ruff, README command flow, platform-drift checks, and workflow YAML parsing pass; real CPython 3.15 compiled wheels built, repaired, validated, and imported on macOS arm64 and Linux aarch64. Native macOS x86_64 and Linux x86_64 builders were unavailable. actionlint and ultrareview are unavailable locally.
2026-10-21 P3: all 25 repeated workflow measurements and the wheel-size run passed perf-gate-ready before and after each run; README plan median was 0.14 s (0.14–0.18), cached synthetic plan 0.14 s (0.13–0.17), with identical outcomes. The final wheel is 40,255 compressed bytes and 150,906 installed bytes; no focused loop exceeded 30 s or showed an actionable regression.
2026-10-21 P4: manual review found and fixed staging-marker crash recovery; 14 focused regressions, all 113 tests, Ruff, the standalone loopback install, and the README flow pass. Core Python source is 4,036 lines. `ultrareview` was unavailable; native macOS x86_64 and Linux x86_64 builders remain untested.

## For the owner afterwards

Review pyproject.toml, packages.toml, the five CLI commands, immutable publication behavior, tests, README, workflows, license, and the final report. No change is complete while a required acceptance proof is missing.
