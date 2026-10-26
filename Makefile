.PHONY: help up down test lint typecheck check llm-smoke warehouse graph demo

help:
	@echo "make up         start local Postgres 16 (waits until healthy)"
	@echo "make down       stop it (data volume kept)"
	@echo "make test       run unit tests"
	@echo "make check      lint + format check + type-check + tests (what CI runs)"
	@echo "make llm-smoke  one cheap real model call through the wrapper (needs ANTHROPIC_API_KEY)"
	@echo "make warehouse  Postgres up + synthetic data + dbt build/freshness/docs + artifacts"
	@echo "make graph      make warehouse, then build the graph file (build/graph.wgz)"
	@echo "make demo       make warehouse, then the Dagster UI at http://localhost:3000"

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

warehouse: up
	uv run warehouse-graph demo-data
	uv run warehouse-graph demo-build

graph: warehouse
	uv run warehouse-graph build

demo: warehouse
	uv run dagster dev -f demo/orchestration/definitions.py
