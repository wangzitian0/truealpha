# TrueAlpha runtime

`truealpha_runtime` owns the boundary between application code and external
runtime dependencies. Applications consume PostgreSQL and the S3 API; they do
not branch on which S3-compatible backend provides object storage.

The entity graph lives in PostgreSQL: `staging.entities`, `staging.entity_aliases`
and `staging.entity_relations` (`docs/entity-identity.md`).

Environment model:

`infra2_sdk.config` owns the tier enum and alias normalization.
`truealpha_runtime.config` exposes `ENVIRONMENT` as `environment` and preserves the
unknown-environment error. Canonical tier names and preview aliases are accepted;
the dependency instances and which tiers require each service remain TrueAlpha-owned.
`tests/test_runtime.py` proves this compatibility against the released SDK (#820).

- Six logical tiers model dependency substitution: `local_dev`, `local_test`,
  `github_ci`, `preview`, `staging`, and `production`.
- The deployed topology covers Local, GitHub CI, Preview (per-PR review via `infra2`), Staging, and Production.
- Local and GitHub CI use app-owned Compose PostgreSQL + RustFS (S3 API).
- Preview, Staging, and Production receive isolated `DATABASE_URL` and `S3_*` values from infra2/Vault (Production stays isolated behind explicit promotion evidence).
- `python -m truealpha_runtime.cli check --live` asserts all declared runtime
  dependencies; absence is a failure, never a silent fallback.

Telemetry (#1034):

Python services call `infra2_sdk.telemetry` directly. It emits structured stdout JSON logs and OTLP/HTTP traces, metrics (including `process.uptime`),
and `INFO+` logs to infra2's shared SigNoz; there is no exporter code in this repository.
It is the application half of infra2 `ops.observability` section 4.2:

- Off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set (or when `OTEL_SDK_DISABLED=true`), so
  local development, CI and every test are unaffected. On the host network, the Dagster
  roles export to the host-loopback collector (`127.0.0.1:14318`, `#1231`); a process with
  no collector access is simply issued no endpoint.
- On, the identity is consumed from infra2's deploy and never defaulted: `OTEL_SERVICE_NAME`
  plus `deployment.environment.name`, `infra.service.id`, `service.version` and
  `infra.iac.ref` in `OTEL_RESOURCE_ATTRIBUTES`. A missing key refuses startup by name.
  `ENVIRONMENT` is not consulted for OTEL identity: a preview stack
  runs with `ENVIRONMENT=staging` and its own `deployment.environment.name` (`pr-12`,
  `canary-preview`, ...), which is exported as issued.
- Services with HTTP APIs (`llm_service.main`) call `infra2_sdk.service.init_service` for
  FastAPI instrumentation, `X-Request-ID`, `/livez`, `/readyz`, `/health`, and probe suppression;
  worker processes (`data_engine.dagster_defs`) call `infra2_sdk.telemetry.init_worker`. The Next.js app is not
  a Python service and is out of scope.
- Complements init.md rule 9: Dagster's UI remains the surface for pipeline runs; this adds
  application-level signals (request errors, latency, logs) that infra2 can alert on.
