.PHONY: help install bootstrap runtime-up runtime-down runtime-check stack-up db-up db-migrate db-reset db-check db-down web llm strategy-smoke vendor-smoke smoke-deps lint format typecheck-python typecheck-web test-python test-web contract-conformance check clean

COMPOSE ?= podman compose

help:
	@echo "TrueAlpha — Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make bootstrap    Full physical bootstrap: uv sync + bun install"
	@echo "  make install      uv sync + bun install + per-commit hook"
	@echo "  make runtime-up   Start Postgres/KG + S3 and create the raw bucket"
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
	@echo "  make strategy-smoke Preview replay of large_model_value_v0 against #335's golden fixture"
	@echo "  make vendor-smoke   Live vendor contract smoke (ARGS=... for flags)"
	@echo "  make smoke-deps     Run gate, dependency, smoke, and contract Python tests"
	@echo ""
	@echo "Before a PR (Local pre cell, 30 seconds):"
	@echo "  make check FILES=<test paths>  static checks on the changed files + the named tests"
	@echo ""
	@echo "CI entries (CI runs the full suite; do not run these whole on a laptop):"
	@echo "  make lint | typecheck-python | typecheck-web | contract-conformance | test-web"
	@echo "  make test-python FILES=<test paths>  pytest on the named files; FILES is required"

bootstrap:
	uv sync --all-packages
	cd apps/app-web && bun install --frozen-lockfile
	git config core.hooksPath tools/git-hooks

install:
	uv sync --all-packages
	cd apps/app-web && bun install
	git config core.hooksPath tools/git-hooks

runtime-up:
	$(COMPOSE) up -d --wait postgres s3
	S3_ENDPOINT=$${S3_ENDPOINT:-http://localhost:9000} S3_ACCESS_KEY=$${S3_ACCESS_KEY:-s3local} \
		S3_SECRET_KEY=$${S3_SECRET_KEY:-s3_local_secret} \
		uv run --package truealpha-runtime truealpha-runtime ensure-bucket

# #447 preflight: llm-service owns SECRET_KEY and refuses deployed starts
# without it. Compose reads .env itself; make does not, hence the file fallback.
stack-up:
	@if [ -z "$${SECRET_KEY}" ] && ! grep -qE '^SECRET_KEY=.+' .env 2>/dev/null; then \
		echo "SECRET_KEY is not set (environment or .env). llm-service owns SECRET_KEY — see .env.example"; \
		exit 1; \
	fi
	$(COMPOSE) --profile app up -d --build --wait

runtime-check:
	uv run --package truealpha-runtime truealpha-runtime check --live

runtime-down:
	$(COMPOSE) --profile app down

db-up: runtime-up

db-down: runtime-down

# One decision about WHERE the local database is, shared by db-migrate and db-reset —
# a second copy of it is how seven migration appliers happened (#984). Prefers the
# compose container so a machine with no host psql still migrates; otherwise talks to
# whatever DATABASE_URL names, defaulting to the conventional local postgres.
define local_db_target
if [ -n "$$($(COMPOSE) ps -q postgres 2>/dev/null)" ]; then \
	export TRUEALPHA_PSQL="$(COMPOSE) exec -T postgres psql"; \
	export DATABASE_URL="postgresql:///$${POSTGRES_DB:-truealpha}?user=$${POSTGRES_USER:-postgres}"; \
else \
	export DATABASE_URL="$${DATABASE_URL:-postgresql://postgres@127.0.0.1:55432/truealpha}"; \
fi
endef

# The initdb mount in docker-compose.yml only runs on a FRESH volume, so an existing dev
# database never picks up new migration files by itself and this target is how it does.
#
# What replay actually is: every file in the chain re-applied, in order, over whatever
# the database already holds. That fixes a database that is BEHIND the chain. It does
# not fix one that is beside it. A `create ... if not exists` does not reshape a
# relation that already exists, so a relation left in a superseded shape keeps that shape: the `create` is
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
# (the compose Postgres publishes 127.0.0.1:55432 by default).
db-check:
	uv run python -m truealpha_tools.schema_drift --database-url "$${DATABASE_URL:-postgresql://postgres@127.0.0.1:55432/truealpha}"

web:
	cd apps/app-web && bun run dev

llm:
	uv run --package truealpha-llm-service uvicorn llm_service.main:app --reload --port 8000

strategy-smoke:
	uv run --package truealpha-data-engine python apps/data-engine/scripts/run_strategy_smoke.py --output-dir .local/strategy-smoke

vendor-smoke:
	uv run --package truealpha-data-engine python apps/data-engine/scripts/vendor_contract_smoke.py $(ARGS)

smoke-deps:
	uv run pytest -m "gate or deps or smoke or contract"

lint:
	uv run ruff check apps common

format:
	uv run ruff check apps common --fix
	uv run ruff format apps common

typecheck-python:
	uv run mypy

typecheck-web:
	cd apps/app-web && bun run typecheck

# An empty FILES made pytest run the whole suite (about 3,150 tests). CI passes one shard.
test-python:
	@test -n "$(strip $(FILES))" || { echo "test-python: set FILES=<test paths>. CI runs the full suite." >&2; exit 2; }
	uv run pytest $(FILES)

test-web:
	cd apps/app-web && bun test

contract-conformance:
	uv run python common/contracts/conformance/export_environment_contract.py --check
	uv run python common/contracts/conformance/export_strategy_definitions.py --check

# The one pre-PR command: static checks on the changed files, then the named tests.
check:
	@tools/prepush.sh $(BASE)
	@if [ -n "$(strip $(FILES))" ]; then uv run pytest $(FILES); fi

clean:
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -exec rm -rf {} + 2>/dev/null || true
	rm -rf apps/app-web/.next
	@echo "✅ Cleaned"
