# TrueAlpha runtime

`truealpha_runtime` owns the boundary between application code and external
runtime dependencies. Applications consume PostgreSQL and the S3 API; they do
not branch on whether infra2, Docker Compose, MinIO, or another S3-compatible
backend provides those services.

The logical graph store is `staging.kg_entities`/`staging.kg_edges` in
PostgreSQL, as required by `init.md`. It is probed independently so a reachable
database with missing KG migrations still fails runtime validation.

Environment model:

`infra2_sdk.runtime.environment` owns the tier enum and alias normalization.
`truealpha_runtime.tiers` preserves the application's import path, `app_env=` keyword
and unknown-environment error. Canonical tier names and preview aliases are accepted;
the dependency instances and which tiers require each service remain TrueAlpha-owned.
`tests/test_runtime.py` proves this compatibility against the released SDK (#820).

- Six logical tiers model dependency substitution: `local_dev`, `local_test`,
  `github_ci`, `preview`, `staging`, and `production`.
- The target rollout uses four actual environments: Local (covering both local
  tiers), GitHub CI, Staging, and Production. This is a target topology, not a
  readiness claim.
- Local and GitHub CI use app-owned Compose PostgreSQL + MinIO.
- Persistent Staging enters during Gate 1. Production is initialized only as an
  isolated Gate 4 shadow and remains non-authoritative until deployed-consumer,
  natural-cadence SLO, curated-universe, and human-graduation evidence pass. Both
  receive isolated `DATABASE_URL` and `S3_*` values from infra2/Vault.
- Preview remains unprovisioned until the Web application needs per-PR visual review.
- `python -m truealpha_runtime.cli check --live` asserts all declared runtime
  dependencies; absence is a failure, never a silent fallback.
