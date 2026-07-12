# Build from the repository root so uv can resolve all workspace packages.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
COPY libs/contracts ./libs/contracts
COPY libs/factors ./libs/factors
COPY libs/runtime ./libs/runtime
COPY apps/data-engine/pyproject.toml ./apps/data-engine/pyproject.toml
COPY apps/data-engine/src ./apps/data-engine/src

# The lock contains this workspace member even though data-engine does not
# depend on it. A stub keeps resolution exact without installing the service.
COPY apps/llm-service/pyproject.toml ./apps/llm-service/pyproject.toml
RUN mkdir -p apps/llm-service/src/llm_service && touch apps/llm-service/src/llm_service/__init__.py

RUN uv sync --frozen --no-dev --no-editable --package truealpha-data-engine

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
RUN groupadd --gid 10001 truealpha \
    && useradd --uid 10001 --gid truealpha --create-home truealpha \
    && mkdir -p /opt/dagster/config /var/lib/dagster/artifacts /var/lib/dagster/compute_logs \
    && chown -R truealpha:truealpha /var/lib/dagster

COPY --from=build /app/.venv /app/.venv
COPY apps/data-engine/dagster/dagster.yaml /opt/dagster/config/dagster.yaml
COPY apps/data-engine/dagster/workspace.yaml /opt/dagster/config/workspace.yaml
COPY db /app/db

ENV PATH="/app/.venv/bin:$PATH" \
    DAGSTER_HOME=/opt/dagster/config \
    DAGSTER_ARTIFACT_DIR=/var/lib/dagster/artifacts \
    DAGSTER_COMPUTE_LOG_DIR=/var/lib/dagster/compute_logs \
    PYTHONUNBUFFERED=1

USER truealpha
EXPOSE 3001
CMD ["dagster-webserver", "-m", "data_engine.dagster_defs", "--host", "127.0.0.1", "--port", "3001"]
