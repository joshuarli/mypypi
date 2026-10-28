.PHONY: lint

lint:
	uv run --locked --python 3.15 ruff check src tests
