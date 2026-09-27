.PHONY: install test lint format run docker update-spec

install:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check src tests examples
	uv run ruff format --check src tests examples

format:
	uv run ruff check --fix src tests examples
	uv run ruff format src tests examples

run:
	uv run gmail-mock --seed examples/seed.json

docker:
	docker build -t gmail-mock:latest .

update-spec:
	./scripts/update_spec.sh
	uv run pytest -q tests/test_catalog.py
