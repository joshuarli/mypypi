# mypypi

Build and publish validated Python wheels to a static package index.

## Checks

```sh
uv run --locked --python 3.15 ruff check src tests
uv run --locked --python 3.15 pytest -q -m "not network" tests
uv run --locked --python 3.15 pytest -q -m network tests/integration/test_uv_resolution.py
```

## Invariants

- CPython 3.15 is the only supported interpreter; keep `project.requires-python` authoritative.
- Source builds and import validation keep their native-target checks.
- Published wheel filenames are immutable and complete index generations are promoted atomically.

## Documentation

- User installation, configuration, operation, and safety: [README.md](README.md).
