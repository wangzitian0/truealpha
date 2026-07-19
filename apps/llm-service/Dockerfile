# Built from the REPO ROOT (build context = .) so the uv workspace can resolve
# the truealpha-factors path dependency:
#   docker build -f apps/llm-service/Dockerfile .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
COPY libs/contracts ./libs/contracts
COPY libs/factors ./libs/factors
COPY libs/runtime ./libs/runtime
COPY apps/llm-service ./apps/llm-service
# data-engine is a workspace member referenced by uv.lock; a stub pyproject
# satisfies the resolver without pulling its dependencies into this image.
COPY apps/data-engine/pyproject.toml ./apps/data-engine/pyproject.toml
RUN mkdir -p apps/data-engine/src/data_engine && touch apps/data-engine/src/data_engine/__init__.py
# --no-editable: install workspace packages as built wheels so the venv is
# self-contained and survives being copied into the runtime stage.
RUN uv sync --frozen --no-dev --no-editable --package truealpha-llm-service

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
# psql: apply_migrations.sh below shells out to it (same `-v ON_ERROR_STOP=1 -f`
# semantics as Makefile's db-migrate target and ci-db.yml — proven, not reinvented).
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*
# Schema DDL is app-owned (db/migrations/*.sql + db/roles.sql, idempotent) but has no
# in-container runner elsewhere yet (#428) — bake it into this image so a boot can apply
# it via $DATABASE_URL, whether that points at a preview's ephemeral DB or the real
# staging/prod truealpha-postgres.
COPY db/migrations ./db/migrations
COPY db/roles.sql ./db/roles.sql
COPY db/apply_migrations.sh ./db/apply_migrations.sh
EXPOSE 8000
CMD ["sh", "-c", "./db/apply_migrations.sh && exec uvicorn llm_service.main:app --host 0.0.0.0 --port 8000"]
