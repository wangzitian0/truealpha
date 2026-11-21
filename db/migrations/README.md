# Migration file names

`db/apply_migrations.sh` applies `db/migrations/*.sql` in shell glob order, then
`db/roles.sql`. It is the only thing that does: CI, the compose initdb hook, the
Makefile, the VPS bootstrap and every llm-service container boot all run that one file
(#984, `common/runtime/tests/test_migration_applier.py`). The filename is the ordering
key, and it is permanent once an environment has run it.

Two forms are legal; `common/runtime/tests/test_migration_chain.py` enforces both.

| form | status | example |
|---|---|---|
| `000N_<schema>.sql` | the squashed baseline, frozen to `0001_raw.sql` .. `0004_app.sql` (#1726, #1775) | `0002_staging.sql` |
| `YYYYMMDDTHHMM_<lane>_<slug>.sql` | every new migration | `20261120T1252_runtime_retire_entity_graph_views.sql` |

* The timestamp is the UTC minute you created the file (`date -u +%Y%m%dT%H%M`). It must
  not precede the newest timestamp on `origin/main` (#1774).
* `<lane>` is your workspace name from the PR title prefix: `datahub`, `factors`,
  `bt`, `web`, `llm`, `runtime`.
* Lower-case `a-z0-9_` only in the slug.

No ledger records which files ran. Every boot replays the whole chain, so each statement
must be idempotent and guarded by a catalog check (`to_regclass(...)`), and a drop must
run only while its object exists (`test_migration_boot_locks.py`).

A timestamped file can fold into the baseline after every environment carries its effect.
Move the statement into the baseline file, delete the timestamped file, and keep the
resulting definition identical, so `make db-check` shows no difference.
