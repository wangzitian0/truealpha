# TrueAlpha runtime

`truealpha_runtime` owns the boundary between application code and external
runtime dependencies. Applications consume PostgreSQL and the S3 API; they do
not branch on whether infra2, Docker Compose, MinIO, or another S3-compatible
backend provides those services.

The logical graph store is `staging.kg_entities`/`staging.kg_edges` in
PostgreSQL, as required by `init.md`. It is probed independently so a reachable
database with missing KG migrations still fails runtime validation.

Environment tiers:

- Local development, local tests, and GitHub CI use Compose PostgreSQL + MinIO.
- Preview, staging, and production must receive the same `DATABASE_URL` and
  `S3_*` contract from infra2/Vault before their runtime smoke can pass.
- `python -m truealpha_runtime.cli check --live` asserts all declared runtime
  dependencies; absence is a failure, never a silent fallback.
