.PHONY: help install db-up db-down web llm sample lint format typecheck test check clean

help:
	@echo "TrueAlpha — Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install      uv sync + bun install + pre-commit hooks"
	@echo "  make db-up        Start dev Postgres (applies db/ DDL on first run)"
	@echo "  make db-down      Stop dev Postgres"
	@echo ""
	@echo "Run:"
	@echo "  make web          Next.js dev server (apps/app-web)"
	@echo "  make llm          FastAPI dev server  (apps/llm-service, :8000)"
	@echo "  make sample       Phase -1: pull SEC company-facts samples"
	@echo ""
	@echo "Quality:"
	@echo "  make check        lint + typecheck + test"

install:
	uv sync --all-packages
	cd apps/app-web && bun install
	uvx pre-commit install 2>/dev/null || true

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

web:
	cd apps/app-web && bun run dev

llm:
	uv run --package truealpha-llm-service uvicorn llm_service.main:app --reload --port 8000

sample:
	uv run --package truealpha-data-engine python apps/data-engine/scripts/pull_sec_samples.py

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check . --fix
	uv run ruff format .

typecheck:
	cd apps/app-web && bun run typecheck

test:
	uv run pytest

check: lint typecheck test
	@echo "✅ All checks passed"

clean:
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -exec rm -rf {} + 2>/dev/null || true
	rm -rf apps/app-web/.next
	@echo "✅ Cleaned"
