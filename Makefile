.PHONY: help up down test lint typecheck check llm-smoke

help:
	@echo "make up         start local Postgres 16 (waits until healthy)"
	@echo "make down       stop it (data volume kept)"
	@echo "make test       run unit tests"
	@echo "make check      lint + format check + type-check + tests (what CI runs)"
	@echo "make llm-smoke  one cheap real model call through the wrapper (needs ANTHROPIC_API_KEY)"

up:
	docker compose up -d --wait

down:
	docker compose down

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

check: lint typecheck test

llm-smoke:
	uv run warehouse-graph llm-smoke
