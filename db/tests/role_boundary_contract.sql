-- #998: no SECURITY DEFINER routine outside the mart boundary may be callable by PUBLIC.
--
-- A definer routine executes with its owner's privileges. PostgreSQL grants EXECUTE on every
-- new routine to PUBLIC, so a definer helper in `staging` is, by default, a read path into
-- `staging.*` for any role holding USAGE on that schema -- which `db/roles.sql` gives to
-- `app_runtime` (the public web application's role, documented there as having no staging
-- access) and to `app_ops_reader`. #996 introduced exactly that shape while fixing a
-- different permission defect, and nothing failed.
--
-- The assertion is over the PROPERTY, not over the two routines #996 named: a definer helper
-- added later must be locked down by `db/roles.sql` or turn this red.
--
-- `proacl` is NULL until someone grants or revokes, and a NULL acl means the built-in default
-- -- which includes PUBLIC EXECUTE. Reading `proacl` alone would therefore find nothing on a
-- brand-new routine, reporting the most exposed case as clean. `acldefault('f', proowner)`
-- supplies what NULL stands for, so the check sees the privilege that is actually in force.
begin;

do $$
declare
    public_callable text;
begin
    select string_agg(routine, ', ' order by routine)
      into public_callable
    from (
        select p.oid::regprocedure::text as routine
        from pg_proc p
        join pg_namespace n on n.oid = p.pronamespace
        cross join lateral aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) entry
        where n.nspname = 'staging'
          and p.prosecdef
          and entry.grantee = 0                      -- 0 is PUBLIC
          and entry.privilege_type = 'EXECUTE'
    ) leaked;

    if public_callable is not null then
        raise exception
            'SECURITY DEFINER routine(s) in schema staging are callable by PUBLIC: %',
            public_callable;
    end if;
end;
$$;

-- The lockdown must not have cut off the reader it exists for: mart.entity_identity's view
-- body calls both helpers, and EXECUTE is checked against the calling role even inside a
-- view. A revoke with no matching grant would leave mart_readonly unable to read the view at
-- all -- the very failure #996 fixed -- so assert the grant, not only the revoke.
do $$
declare
    routine text;
begin
    foreach routine in array array[
        'staging.entity_survivor(uuid, timestamptz)',
        'staging.entity_alias_valid_to(bigint, timestamptz)'
    ] loop
        if to_regprocedure(routine) is null then
            raise exception 'mart.entity_identity helper % is missing from the chain', routine;
        end if;
        if not has_function_privilege('mart_readonly', to_regprocedure(routine), 'EXECUTE') then
            raise exception 'mart_readonly cannot execute %, so it cannot read mart.entity_identity', routine;
        end if;
        if not has_function_privilege('app_ops_reader', to_regprocedure(routine), 'EXECUTE') then
            raise exception 'app_ops_reader cannot execute %, so it cannot read mart.entity_identity', routine;
        end if;
    end loop;
end;
$$;

rollback;
