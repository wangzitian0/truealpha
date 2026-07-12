-- A capture scope is a stable promise across scheduled cycles. The evaluated
-- cutoff belongs to each manifest/run; storing it on the scope would change the
-- capture_scope_id every cycle and make two-run evidence incomparable.
do $$
begin
    if exists (select 1 from staging.capture_manifests limit 1) then
        raise exception 'capture manifests exist without an evaluated cutoff; replay them before migration 0010';
    end if;
end $$;

alter table staging.capture_manifests add column if not exists evaluated_as_of timestamptz;
alter table staging.capture_manifests alter column evaluated_as_of set not null;
