.PHONY: help install runtime-up runtime-down runtime-check stack-up db-up db-migrate db-down web llm sample sample-evidence sample-audit strategy-smoke lint format typecheck test contract-conformance check clean

help:
	@echo "TrueAlpha — Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install      uv sync + bun install + pre-commit hooks"
	@echo "  make runtime-up   Start Postgres/KG + MinIO and create the raw bucket"
	@echo "  make stack-up     Build/start runtime + web + llm-service"
	@echo "  make runtime-check Probe Postgres, KG tables, and object storage"
	@echo "  make runtime-down Stop the local stack (keeps volumes)"
	@echo "  make db-migrate   Re-apply db/ DDL to a running Postgres (idempotent)"
	@echo ""
	@echo "Run:"
	@echo "  make web          Next.js dev server (apps/app-web)"
	@echo "  make llm          FastAPI dev server  (apps/llm-service, :8000)"
	@echo "  make sample       Phase -1: pull SEC company-facts samples"
	@echo "  make sample-evidence Capture the bounded issue #14 public evidence set"
	@echo "  make sample-audit Check fixture readiness for tooling and backtests"
	@echo "  make strategy-smoke Preview replay of large_model_value_v0 against #335's golden fixture"
	@echo ""
	@echo "Quality:"
	@echo "  make check        lint + typecheck + test"
	@echo "  make contract-conformance Verify Python/TypeScript contract parity"

install:
	uv sync --all-packages
	cd apps/app-web && bun install
	uvx pre-commit install 2>/dev/null || true

runtime-up:
	docker compose up -d --wait postgres minio
	docker compose run --rm minio-init

# #447 preflight: the app-web image runs NODE_ENV=production and refuses to
# serve without a real SECRET_KEY; without this check an unset key surfaces
# only as a healthcheck timeout after a full build. Compose reads .env itself;
# make does not, hence the file fallback.
stack-up:
	@if [ -z "$${SECRET_KEY}" ] && ! grep -qE '^SECRET_KEY=.+' .env 2>/dev/null; then \
		echo "SECRET_KEY is not set (environment or .env). app-web runs NODE_ENV=production and refuses to serve without a real key — see .env.example"; \
		exit 1; \
	fi
	docker compose --profile app up -d --build --wait

runtime-check:
	uv run --package truealpha-runtime truealpha-runtime check --live

runtime-down:
	docker compose --profile app down

db-up: runtime-up

db-down: runtime-down

# The initdb mount in docker-compose.yml only runs on a FRESH volume — an existing
# dev DB never picks up new migration files by itself. All DDL is `if not exists`,
# so re-applying everything is safe and cheap.
# Prefers the compose container; falls back to host psql (DATABASE_URL or the
# conventional local postgres) so a docker-less machine can still migrate.
db-migrate:
	@if [ -n "$$(docker compose ps -q postgres 2>/dev/null)" ]; then \
		for f in db/migrations/*.sql db/roles.sql; do \
			echo "== $$f"; \
			docker compose exec -T postgres psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-truealpha} -v ON_ERROR_STOP=1 < $$f || exit 1; \
		done; \
	else \
		for f in db/migrations/*.sql db/roles.sql; do \
			echo "== $$f"; \
			psql "$${DATABASE_URL:-postgresql://postgres@127.0.0.1:5432/truealpha}" -v ON_ERROR_STOP=1 -f $$f || exit 1; \
		done; \
	fi

web:
	cd apps/app-web && bun run dev

llm:
	uv run --package truealpha-llm-service uvicorn llm_service.main:app --reload --port 8000

sample:
	uv run --package truealpha-data-engine python apps/data-engine/scripts/pull_sec_samples.py

sample-evidence:
	uv run --package truealpha-data-engine python apps/data-engine/scripts/capture_strategy_evidence.py --resume

sample-audit:
	uv run --package truealpha-data-engine python apps/data-engine/scripts/audit_strategy_samples.py

strategy-smoke:
	uv run --package truealpha-data-engine python apps/data-engine/scripts/run_strategy_smoke.py --output-dir .local/strategy-smoke

lint:
	uv run ruff check apps libs
	uv run ruff format --check apps libs

format:
	uv run ruff check apps libs --fix
	uv run ruff format apps libs

typecheck:
	uv run mypy
	cd apps/app-web && bun run typecheck

test:
	uv run pytest

contract-conformance:
	uv run python libs/contracts/conformance/export_issue58.py --check
	# Runs every apps/app-web/tests/*.test.ts file, mirroring ci-web.yml (#373 found a
	# hardcoded list here had silently stopped running 5 of them: conversations/documents
	# validation, and now #433's topt tests — a hardcoded list drifts every time a test
	# file is added and this line isn't touched; the glob can't drift).
	# -eu only (no pipefail): make's default shell is /bin/sh (dash on most Linux, no
	# `-o pipefail` support), and this loop has no pipe to protect anyway (Copilot review).
	cd apps/app-web && set -eu; for f in tests/*.test.ts; do echo "== $$f"; bun run "$$f"; done

check: lint typecheck test contract-conformance
	@echo "✅ All checks passed"

clean:
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -exec rm -rf {} + 2>/dev/null || true
	rm -rf apps/app-web/.next
	@echo "✅ Cleaned"
