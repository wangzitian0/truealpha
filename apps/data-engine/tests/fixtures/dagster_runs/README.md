# Dagster run history fixture

`staging_2026-10-21.csv` is the finished run history of the staging Dagster database as of
2026-10-21 10:06 UTC. Each row is one run in the state SUCCESS or FAILURE since 2026-09-20.

- `job`: the job name (`dagster.runs.pipeline_name`).
- `status`: `SUCCESS` or `FAILURE`.
- `created_at_utc`: `dagster.runs.create_timestamp`, UTC.
- `scheduled`: `true` when the run has the tag key `schedule_name` with the `dagster` prefix.

The file holds no run id, no log text and no configuration. A read-only SQL query produced it.
The runs from 2026-10-20 12:00 UTC are the 68 runs of the staging report of 2026-10-21: 62 SUCCESS and 6 FAILURE.
