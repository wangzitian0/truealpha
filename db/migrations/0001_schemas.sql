-- The four schemas (init.md Section 6).
-- dagster: Dagster's own run/event/schedule storage — explicitly configured here,
-- not left on its default local store (init.md Section 1, rule 9).

create schema if not exists raw;
create schema if not exists staging;
create schema if not exists mart;
create schema if not exists dagster;
