# Migration file names

`db/apply_migrations.sh`, `db/docker-init.sh`, `Makefile db-migrate` and `ci-db.yml`
all apply `db/migrations/*.sql` in shell glob order on every boot. The filename is
the ordering key, and it is permanent once an environment has run it.

Two forms are legal; `libs/runtime/tests/test_migration_chain.py` enforces both.

| form | status | example |
|---|---|---|
| `00NN_<slug>.sql` | frozen at `0048`, no new files | `0048_per_isin_ticker_crosswalk.sql` |
| `YYYYMMDDTHHMM_<lane>_<slug>.sql` | every new migration | `20260905T0930_datahub_holdings_vintage.sql` |

* The timestamp is the UTC minute you created the file (`date -u +%Y%m%dT%H%M`).
* `<lane>` is your workspace name from the PR title prefix: `datahub`, `factors`,
  `bt`, `web`, `llm`, `runtime`.
* Lower-case `a-z0-9_` only in the slug.

Why: four lanes added 47 migrations in the repository's first sixty days and took
"the next number" at the same time seven times (`0019 0029 0030 0031 0037 0039 0040`,
frozen in the test as `KNOWN_COLLISIONS`). A timestamp plus a lane name cannot
collide, and because `"2"` sorts after `"0"` every timestamped file applies after the
whole legacy chain, so nothing already deployed reorders (#576, #731).

Never rename a migration that has reached staging or production.
