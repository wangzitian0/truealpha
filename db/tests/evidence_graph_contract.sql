\set ON_ERROR_STOP on

begin;

-- A valid node inserts.
insert into staging.evidence_nodes
    (node_id, kind, content_sha256, valid_from, transaction_time, recorded_at)
values ('capture-run:' || repeat('a', 64), 'capture_run', repeat('a', 64),
        '2026-03-31', now(), now());

-- Append-only: update and delete are rejected.
do $$ begin
    begin
        update staging.evidence_nodes set kind = 'snapshot'
        where node_id = 'capture-run:' || repeat('a', 64);
        raise exception 'append-only trigger did not reject an update';
    exception when raise_exception then null;
    end;
end $$;

-- The content_sha256 integrity column must equal the id suffix.
do $$ begin
    begin
        insert into staging.evidence_nodes
            (node_id, kind, content_sha256, valid_from, transaction_time, recorded_at)
        values ('capture-run:' || repeat('b', 64), 'capture_run', repeat('c', 64),
                '2026-03-31', now(), now());
        raise exception 'id/hash integrity check did not fire';
    exception when check_violation then null;
    end;
end $$;

-- The id prefix must match the node kind.
do $$ begin
    begin
        insert into staging.evidence_nodes
            (node_id, kind, content_sha256, valid_from, transaction_time, recorded_at)
        values ('snapshot:' || repeat('d', 64), 'capture_run', repeat('d', 64),
                '2026-03-31', now(), now());
        raise exception 'kind/prefix check did not fire';
    exception when check_violation then null;
    end;
end $$;

-- An edge cannot reference a node that does not exist (FK lineage integrity).
do $$ begin
    begin
        insert into staging.evidence_edges
            (edge_id, content_sha256, from_kind, from_id, to_kind, to_id, relation,
             valid_from, transaction_time, recorded_at)
        values ('evidence-edge:' || repeat('e', 64), repeat('e', 64),
                'normalized_observation', 'normalized-observation:' || repeat('9', 64),
                'capture_run', 'capture-run:' || repeat('a', 64), 'derived_from',
                '2026-03-31', now(), now());
        raise exception 'edge foreign key did not fire';
    exception when foreign_key_violation then null;
    end;
end $$;

-- The governed pointer advances forward only: a duplicate (key, sequence) is rejected.
insert into mart.current_pointer
    (pointer_id, content_sha256, environment, universe_id, universe_version, factor_id,
     target_run_id, sequence, previous_run_id, advanced_at)
values ('current-pointer:' || repeat('a', 64), repeat('a', 64), 'local_test',
        'universe:x', 'v1', 'f', 'capture-run:' || repeat('a', 64), 0, null, now());

do $$ begin
    begin
        insert into mart.current_pointer
            (pointer_id, content_sha256, environment, universe_id, universe_version, factor_id,
             target_run_id, sequence, previous_run_id, advanced_at)
        values ('current-pointer:' || repeat('f', 64), repeat('f', 64), 'local_test',
                'universe:x', 'v1', 'f', 'capture-run:' || repeat('a', 64), 0, null, now());
        raise exception 'duplicate advance sequence was not rejected';
    exception when unique_violation then null;
    end;
end $$;

-- The head view returns the latest advance.
insert into staging.evidence_nodes
    (node_id, kind, content_sha256, valid_from, transaction_time, recorded_at)
values ('capture-run:' || repeat('e', 64), 'capture_run', repeat('e', 64),
        '2026-03-31', now(), now());
insert into mart.current_pointer
    (pointer_id, content_sha256, environment, universe_id, universe_version, factor_id,
     target_run_id, sequence, previous_run_id, advanced_at)
values ('current-pointer:' || repeat('b', 64), repeat('b', 64), 'local_test',
        'universe:x', 'v1', 'f', 'capture-run:' || repeat('e', 64), 1,
        'capture-run:' || repeat('a', 64), now());

do $$
declare head_run text;
begin
    select target_run_id into head_run from mart.current_pointer_head
    where environment = 'local_test' and universe_id = 'universe:x'
      and universe_version = 'v1' and factor_id = 'f';
    if head_run <> 'capture-run:' || repeat('e', 64) then
        raise exception 'head view did not return the latest advance: %', head_run;
    end if;
end $$;

rollback;
