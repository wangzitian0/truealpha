.PHONY: help install runtime-up runtime-down runtime-check stack-up db-up db-down web llm sample lint format typecheck test check clean

help:
	@echo "TrueAlpha — Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install      uv sync + bun install + pre-commit hooks"
	@echo "  make runtime-up   Start Postgres/KG + MinIO and create the raw bucket"
	@echo "  make stack-up     Build/start runtime + web + llm-service"
	@echo "  make runtime-check Probe Postgres, KG tables, and object storage"
	@echo "  make runtime-down Stop the local stack (keeps volumes)"
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

runtime-up:
	docker compose up -d --wait postgres minio
	docker compose run --rm minio-init

stack-up:
	docker compose --profile app up -d --build --wait

runtime-check:
	uv run --package truealpha-runtime truealpha-runtime check --live

runtime-down:
	docker compose --profile app down

db-up: runtime-up

db-down: runtime-down

web:
	cd apps/app-web && bun run dev

llm:
	uv run --package truealpha-llm-service uvicorn llm_service.main:app --reload --port 8000

sample:
	uv run --package truealpha-data-engine python apps/data-engine/scripts/pull_sec_samples.py

lint:
	uv run ruff check apps libs
	uv run ruff format --check apps libs

format:
	uv run ruff check apps libs --fix
	uv run ruff format apps libs

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
