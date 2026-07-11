# TrueAlpha runtime

`truealpha_runtime` owns the boundary between application code and external
runtime dependencies. Applications consume PostgreSQL and the S3 API; they do
not branch on whether infra2, Docker Compose, MinIO, or another S3-compatible
backend provides those services.

The logical graph store is `staging.kg_entities`/`staging.kg_edges` in
PostgreSQL, as required by `init.md`. It is probed independently so a reachable
database with missing KG migrations still fails runtime validation.

Environment model:

- Six logical tiers model dependency substitution: `local_dev`, `local_test`,
  `github_ci`, `preview`, `staging`, and `production`.
- The active rollout provisions only four actual environments: Local (covering
  both local tiers), GitHub CI, Staging, and Production.
- Local and GitHub CI use app-owned Compose PostgreSQL + MinIO.
- Staging begins in Phase 3 and Production remains gated until Phase 6. Both
  must receive isolated `DATABASE_URL` and `S3_*` values from infra2/Vault.
- Preview remains unprovisioned until the Web application needs per-PR visual review.
- `python -m truealpha_runtime.cli check --live` asserts all declared runtime
  dependencies; absence is a failure, never a silent fallback.
