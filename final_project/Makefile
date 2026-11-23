.PHONY: check

check:
	uv run ruff format --check .
	uv run ruff check .
	uv run pyright
	uv run pytest -q
	uv run lint-imports
