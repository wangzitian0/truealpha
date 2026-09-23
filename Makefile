.PHONY: help install bootstrap doctor runtime-up runtime-down runtime-check stack-up db-up db-migrate db-reset db-check db-down web llm sample sample-evidence sample-audit strategy-smoke lint format typecheck test prepush contract-conformance check clean

help:
	@echo "TrueAlpha — Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make bootstrap    Full physical bootstrap: uv sync + bun install + doctor"
	@echo "  make doctor       Verify physical dev_env contracts and dependencies"
	@echo "  make install      uv sync + bun install + pre-commit hooks"
	@echo "  make runtime-up   Start Postgres/KG + MinIO and create the raw bucket"
	@echo "  make stack-up     Build/start runtime + web + llm-service"
	@echo "  make runtime-check Probe Postgres, KG tables, and object storage"
	@echo "  make runtime-down Stop the local stack (keeps volumes)"
	@echo "  make db-migrate   Replay the declared chain onto an existing database"
	@echo "  make db-reset     Recreate the database empty and apply the chain — CI's starting point"
	@echo "  make db-check     Compare a live database's schema against the declared chain"
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

bootstrap:
	uv sync --all-packages
	cd apps/app-web && bun install --frozen-lockfile
	uv run python tools/doctor.py

doctor:
	uv run python tools/doctor.py

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

# One decision about WHERE the local database is, shared by db-migrate and db-reset —
# a second copy of it is how seven migration appliers happened (#984). Prefers the
# compose container so a machine with no host psql still migrates; otherwise talks to
# whatever DATABASE_URL names, defaulting to the conventional local postgres.
define local_db_target
if [ -n "$$(docker compose ps -q postgres 2>/dev/null)" ]; then \
	export TRUEALPHA_PSQL="docker compose exec -T postgres psql"; \
	export DATABASE_URL="postgresql:///$${POSTGRES_DB:-truealpha}?user=$${POSTGRES_USER:-postgres}"; \
else \
	export DATABASE_URL="$${DATABASE_URL:-postgresql://postgres@127.0.0.1:5432/truealpha}"; \
fi
endef

# The initdb mount in docker-compose.yml only runs on a FRESH volume, so an existing dev
# database never picks up new migration files by itself and this target is how it does.
#
# What replay actually is: every file in the chain re-applied, in order, over whatever
# the database already holds. That fixes a database that is BEHIND the chain. It does
# not fix one that is beside it. The chain carries 113 `alter table` and 162 `drop`
# statements against 176 `create ... if not exists` (grep -ohiE over db/migrations/*.sql,
# 2026-09-23), so a relation left in a superseded shape keeps that shape: the `create` is
# a no-op and the `alter` that would have reshaped it already ran, back when this database
# had the shape it was written against. (Before #984 these three lines claimed "All DDL is
# `if not exists`, so re-applying everything is safe and cheap" — measured against the
# chain, that sentence was false, and a local database sat broken for weeks behind it.)
#
# `make db-reset` is the repair path and `make db-check` says which of the two you need.
db-migrate:
	@$(local_db_target); sh db/apply_migrations.sh

# CI's Postgres service declares no volume: every job starts empty and applies the chain
# once. This is that, locally — drop, create, apply, zero seed. The local database is
# CI's starting point retained as a cache, not a second lifecycle (#984).
db-reset:
	@$(local_db_target); sh db/reset_database.sh

# Is this database still the chain? Read-only against the target; it builds a reference
# from db/migrations + db/roles.sql on the same server and diffs the two catalogs, so
# there is no snapshot to keep fresh. Needs host psql and a host-reachable DATABASE_URL
# (the compose Postgres publishes 127.0.0.1:5432 by default).
db-check:
	uv run python tools/schema_drift.py --database-url "$${DATABASE_URL:-postgresql://postgres@127.0.0.1:5432/truealpha}"

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

prepush:
	@tools/prepush.sh $(BASE)

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
	# DB-backed tests skip gracefully without a local Postgres; set
	# TRUEALPHA_REQUIRE_RUNTIME=1 to forbid skips (unreachable DB then fails hard,
	# exactly as ci-web runs them — #468).
	cd apps/app-web && set -eu; for f in tests/*.test.ts; do echo "== $$f"; bun run "$$f"; done

check: lint typecheck test contract-conformance
	@echo "✅ All checks passed"

clean:
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -exec rm -rf {} + 2>/dev/null || true
	rm -rf apps/app-web/.next
	@echo "✅ Cleaned"
