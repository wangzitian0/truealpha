# Built from the REPO ROOT (build context = .) so the uv workspace can resolve
# the truealpha-factors path dependencies:
#   docker build -f apps/data-engine/Dockerfile .
#
# The image ships the exact reviewed `truealpha-data-engine` package as the two
# runtime roles the infra2 Staging/Production compose selects by command
# (truealpha/truealpha/20.data_engine/compose.yaml):
#   dagster-webserver -m data_engine.dagster_defs   (loopback-only UI)
#   dagster-daemon    run -m data_engine.dagster_defs   (sole recurring-run authority, #27)
# Both roles load the same digest-pinned image and the same `data_engine.dagster_defs`
# Definitions, so a schedule the daemon launches is the exact code the webserver renders.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
COPY libs/contracts ./libs/contracts
COPY libs/factors ./libs/factors
COPY libs/runtime ./libs/runtime
COPY apps/data-engine ./apps/data-engine
# --no-editable: install workspace packages as built wheels so the venv is
# self-contained and survives being copied into the runtime stage.
RUN uv sync --frozen --no-dev --no-editable --package truealpha-data-engine

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Dagster run/event/schedule metadata lives in the environment's Postgres
# `dagster` schema (README: "the dagster schema"), reached through
# DAGSTER_POSTGRES_URL, which the infra2 entrypoint exports from DATABASE_URL.
# DAGSTER_HOME must NOT be the mounted data volume (/var/lib/dagster) -- the
# empty volume would shadow a baked dagster.yaml -- so it points at an image
# path holding dagster.yaml, while dagster.yaml sends the local artifact base
# dir to the persistent /var/lib/dagster mount.
ENV DAGSTER_HOME=/opt/dagster
RUN mkdir -p /opt/dagster /var/lib/dagster
COPY apps/data-engine/dagster.yaml /opt/dagster/dagster.yaml
# The output-invariant suite and its exemptions ride along so the quality lane can run
# them inside the environment (#674 / #725 A): a GitHub runner cannot reach the
# loopback Postgres, and a check that only ever examined the CI fixture proved nothing.
COPY tools/output_invariants.py tools/output_invariant_exemptions.json /app/tools/

# The environment contract this image ships (#784, #759). Two readers, one file, at the
# repository path under /app -- the working directory this image already runs in:
#   * infra2's entrypoint boot-validates the process environment against it
#     (`truealpha_runtime.boot.assert_environment`), which it could not do before because
#     the file was simply absent from this image, exactly as `apps/llm-service/Dockerfile`
#     ships its own;
#   * `data_engine.release_identity` fingerprints it into the release manifest id, so a
#     release that changes what the deployment must supply changes its identity.
COPY apps/data-engine/required-env.generated.json ./apps/data-engine/required-env.generated.json
# The migration set the release identity measures (#784). DDL is applied by the llm-service
# image's entrypoint, not here -- this image ships the files only so it can hash the schema
# it was built against, and so the ordered ids in a run's identity are the ones the artifact
# actually carries rather than a list written down somewhere.
COPY db/migrations ./db/migrations

# Default to the webserver role; the compose pins each role's command explicitly.
EXPOSE 3001
CMD ["dagster-webserver", "-m", "data_engine.dagster_defs", "--host", "127.0.0.1", "--port", "3001"]
