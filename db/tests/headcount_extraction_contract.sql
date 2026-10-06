-- #1061: the headcount extraction tables are retired.
-- This file once asserted the shape of staging.headcount_extraction_invocations and staging.headcount_facts.
-- Now it asserts that the chain does not create them again.
-- The live headcount plane is staging.issuer_headcount_facts.
-- ci-db names this file, so it stays and checks the retirement.
begin;

do $$
begin
    if to_regclass('staging.headcount_extraction_invocations') is not null then
        raise exception 'retired table staging.headcount_extraction_invocations exists after the chain';
    end if;
    if to_regclass('staging.headcount_facts') is not null then
        raise exception 'retired table staging.headcount_facts exists after the chain';
    end if;
    if to_regprocedure('staging.validate_headcount_invocation()') is not null
       or to_regprocedure('staging.validate_headcount_projection()') is not null then
        raise exception 'retired headcount trigger function exists after the chain';
    end if;
    if to_regclass('staging.issuer_headcount_facts') is null then
        raise exception 'live headcount plane staging.issuer_headcount_facts is missing';
    end if;
end;
$$;

rollback;
