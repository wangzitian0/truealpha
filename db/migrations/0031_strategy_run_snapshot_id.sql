-- 0031: Bind a strategy run to its exact PIT capture snapshot (#395).
--
-- mart.strategy_runs today binds only corpus_sha256 (the frozen fixture hash), so a
-- run has no point-in-time lineage to the captured inputs it was computed from. Add
-- a nullable snapshot_id: the live gateway-backed writer (#395) populates it with the
-- exact strategy-snapshot identity; existing fixture/preview runs and the current
-- writer leave it null. Backward-compatible -- no existing row or writer breaks.

alter table mart.strategy_runs
    add column if not exists snapshot_id text
        check (snapshot_id is null or snapshot_id ~ '^strategy-snapshot:[0-9a-f]{64}$');

comment on column mart.strategy_runs.snapshot_id is
    'Exact PIT strategy-snapshot the run was computed from (#395); null for fixture/preview runs.';
