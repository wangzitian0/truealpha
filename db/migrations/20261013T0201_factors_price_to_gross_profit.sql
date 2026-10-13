-- #1235: strategy definition 1.0.0 values by P/GP = market value / gross profit.
--
-- The owner confirmed definition 1.0.0 of large_model_value_v0 on 2026-10-12. The tier
-- bands target P/GP, not P/S. Both engines read the one definition: the strategy tick
-- (mart.strategy_decisions) and the TOPT core engine (mart.topt_core_results).
--
-- A P/GP value never goes into a P/S column. This migration adds named P/GP columns and
-- changes no existing row:
--
--   mart.strategy_decisions  current_price_to_gross_profit, target_price_to_gross_profit
--   mart.topt_core_results   current_pgp, target_pgp_lower, target_pgp_upper, target_pgp_midpoint
--
-- Rows written before this change hold NULL in the new columns. Their P/S columns keep
-- their meaning. On a 1.0.0 strategy decision, target_price_to_sales is NULL (no P/S band
-- exists) and current_price_to_sales still holds P/S. On a 1.0.0 TOPT core row,
-- current_ps still holds P/S, and target_ps_* hold the P/S that the P/GP band implies at
-- the issuer's own gross margin. Migration 0030 re-asserts its NOT NULL rule on
-- target_ps_* at every boot, so those columns cannot be NULL on an available row.
--
-- The TOPT core row stamps the strategy definition (strategy-definition:<sha>) as its
-- tier definition, so topt_core_results.tier_definition_sha256 equals
-- strategy_runs.definition_content_sha256 for the definition both engines used.
--
-- Replayed on every boot (#916). Each block reads the catalog first and changes nothing
-- when the change is already present, so a replay takes no table lock. The new CHECK
-- constraints are NOT VALID: they bind new rows and do not scan old ones (old rows hold
-- NULL in the new columns and satisfy them).

do $$
begin
    if not exists (
        select 1
        from pg_attribute
        where attrelid = 'mart.strategy_decisions'::regclass
          and attname = 'current_price_to_gross_profit'
          and not attisdropped
    ) then
        alter table mart.strategy_decisions
            add column if not exists current_price_to_gross_profit numeric;
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_attribute
        where attrelid = 'mart.strategy_decisions'::regclass
          and attname = 'target_price_to_gross_profit'
          and not attisdropped
    ) then
        alter table mart.strategy_decisions
            add column if not exists target_price_to_gross_profit numeric;
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_attribute
        where attrelid = 'mart.topt_core_results'::regclass
          and attname = 'current_pgp'
          and not attisdropped
    ) then
        alter table mart.topt_core_results
            add column if not exists current_pgp numeric;
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_attribute
        where attrelid = 'mart.topt_core_results'::regclass
          and attname = 'target_pgp_lower'
          and not attisdropped
    ) then
        alter table mart.topt_core_results
            add column if not exists target_pgp_lower numeric;
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_attribute
        where attrelid = 'mart.topt_core_results'::regclass
          and attname = 'target_pgp_upper'
          and not attisdropped
    ) then
        alter table mart.topt_core_results
            add column if not exists target_pgp_upper numeric;
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1
        from pg_attribute
        where attrelid = 'mart.topt_core_results'::regclass
          and attname = 'target_pgp_midpoint'
          and not attisdropped
    ) then
        alter table mart.topt_core_results
            add column if not exists target_pgp_midpoint numeric;
    end if;
end
$$;

-- The fund-level weighted gap averages the gaps of one run's core rows, so it divides the
-- multiple those rows divide. The producer records it; NULL on a row written before #1235
-- (all of which divided P/S) and on a refused aggregate, which has no gap.
do $$
begin
    if not exists (
        select 1
        from pg_attribute
        where attrelid = 'mart.fund_virtual_company'::regclass
          and attname = 'valuation_basis'
          and not attisdropped
    ) then
        alter table mart.fund_virtual_company
            add column if not exists valuation_basis text;
    end if;
end
$$;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'mart.fund_virtual_company'::regclass
          and conname = 'fund_virtual_company_valuation_basis_check'
    ) then
        alter table mart.fund_virtual_company
            add constraint fund_virtual_company_valuation_basis_check
                check (
                    valuation_basis in ('price_to_sales', 'price_to_gross_profit')
                    and weighted_valuation_gap is not null
                    or valuation_basis is null
                ) not valid;
    end if;
end
$$;

-- A strategy decision names the target of at most one basis, and a P/GP target comes
-- with its current P/GP.
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'mart.strategy_decisions'::regclass
          and conname = 'strategy_decisions_one_valuation_basis_check'
    ) then
        alter table mart.strategy_decisions
            add constraint strategy_decisions_one_valuation_basis_check
                check (
                    (target_price_to_sales is null or target_price_to_gross_profit is null)
                    and ((current_price_to_gross_profit is null) = (target_price_to_gross_profit is null))
                    and (current_price_to_gross_profit is null or current_price_to_gross_profit > 0)
                ) not valid;
    end if;
end
$$;

-- A TOPT core row carries all four P/GP values or none, and only an available row carries them.
-- num_nonnulls makes "some but not all" a refusal: a comparison on a NULL member is NULL, and a
-- CHECK passes on NULL, so a conjunction of comparisons alone would admit a half-filled row.
-- The guard reads the stored definition, so a database that holds an earlier text of this
-- constraint gets this one; a replay of an identical definition changes nothing.
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'mart.topt_core_results'::regclass
          and conname = 'topt_core_results_pgp_values_check'
          and pg_get_constraintdef(oid) like '%num_nonnulls%'
    ) then
        alter table mart.topt_core_results
            drop constraint if exists topt_core_results_pgp_values_check;
        alter table mart.topt_core_results
            add constraint topt_core_results_pgp_values_check
                check (
                    num_nonnulls(current_pgp, target_pgp_lower, target_pgp_upper, target_pgp_midpoint) = 0
                    or (
                        num_nonnulls(current_pgp, target_pgp_lower, target_pgp_upper, target_pgp_midpoint) = 4
                        and availability = 'available'
                        and current_pgp > 0
                        and target_pgp_lower > 0
                        and target_pgp_lower < target_pgp_upper
                    )
                ) not valid;
    end if;
end
$$;

-- The invocation names the strategy definition that supplied its bands. Rows of the
-- former TOPT-own bands keep their three-tier-definition id.
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'mart.topt_core_invocations'::regclass
          and conname = 'topt_core_invocations_tier_definition_id_check'
          and pg_get_constraintdef(oid) like '%strategy-definition%'
    ) then
        alter table mart.topt_core_invocations
            drop constraint if exists topt_core_invocations_tier_definition_id_check;
        alter table mart.topt_core_invocations
            add constraint topt_core_invocations_tier_definition_id_check
                check (tier_definition_id ~ '^(three-tier-definition|strategy-definition):[0-9a-f]{64}$');
    end if;
end
$$;

-- The insert validator of 0026, with the P/GP members. A 1.0.0 payload carries 37 keys and
-- its four P/GP values must equal the columns. A payload without them (33 keys, the form
-- before #1235) must leave the four P/GP columns NULL, so a producer that predates this
-- change can still write during a rolling deploy and cannot write a P/GP value unchecked.
create or replace function mart.validate_topt_core_result()
returns trigger language plpgsql as $$
declare
    invocation mart.topt_core_invocations%rowtype;
    gppe_result mart.topt_gppe_results%rowtype;
    snapshot staging.topt_core_snapshots%rowtype;
    member staging.topt_core_snapshot_members%rowtype;
    pgp_keys constant text[] := array['current_pgp', 'target_pgp_lower', 'target_pgp_upper', 'target_pgp_midpoint'];
    carries_pgp boolean := new.payload ?| pgp_keys;
begin
    select * into invocation from mart.topt_core_invocations where invocation_id = new.invocation_id;
    select * into gppe_result from mart.topt_gppe_results where result_id = new.gppe_result_id;
    select * into snapshot from staging.topt_core_snapshots where snapshot_id = new.snapshot_id;
    select * into member
      from staging.topt_core_snapshot_members
     where snapshot_id = new.snapshot_id and issuer_id = new.issuer_id;
    if invocation.invocation_id is null
       or snapshot.snapshot_id is null
       or member.snapshot_id is null
       or invocation.snapshot_id <> new.snapshot_id
       or invocation.gppe_invocation_id <> new.gppe_invocation_id
       or gppe_result.result_id is null
       or gppe_result.invocation_id <> new.gppe_invocation_id
       or gppe_result.snapshot_id <> new.snapshot_id
       or gppe_result.issuer_id <> new.issuer_id
       or gppe_result.gppe_definition_id <> new.gppe_definition_id
       or gppe_result.gppe_definition_sha256 <> new.gppe_definition_sha256
       or gppe_result.operating_branch <> new.operating_branch
       or gppe_result.operating_metric <> new.operating_metric
       or gppe_result.input_observation_ids <> new.input_observation_ids
       or gppe_result.confidence <> new.confidence
       or gppe_result.freshness <> new.freshness
       or (gppe_result.availability = 'unavailable' and (
           new.availability <> 'unavailable'
           or gppe_result.reason_codes <> new.reason_codes
       ))
       or (new.operating_efficiency is not null
           and gppe_result.operating_efficiency is distinct from new.operating_efficiency)
       or (new.availability = 'available' and (
           gppe_result.availability <> 'available'
           or gppe_result.capital_adjusted_gross_profit is distinct from new.capital_adjusted_gross_profit
           or gppe_result.gppe is distinct from new.gppe
       ))
       or invocation.gppe_definition_id <> new.gppe_definition_id
       or invocation.gppe_definition_sha256 <> new.gppe_definition_sha256
       or invocation.tier_definition_id <> new.tier_definition_id
       or invocation.tier_definition_sha256 <> new.tier_definition_sha256
       or snapshot.run_id <> new.run_id
       or snapshot.release_manifest_id <> new.release_manifest_id
       or snapshot.universe_id <> new.universe_id
       or snapshot.universe_version <> new.universe_version
       or snapshot.universe_sha256 <> new.universe_sha256
       or snapshot.cutoff <> new.cutoff
       or member.instrument_id <> new.instrument_id
       or member.listing_id <> new.listing_id
       or member.observation_ids <> new.input_observation_ids
       or (select count(*) from jsonb_object_keys(new.payload)) <> (case when carries_pgp then 37 else 33 end)
       or not (new.payload ?& array[
           'invocation_id', 'snapshot_id', 'run_id', 'release_manifest_id',
           'universe_id', 'universe_version', 'universe_sha256', 'cutoff',
           'issuer_id', 'instrument_id', 'listing_id', 'operating_branch',
           'operating_metric', 'availability', 'operating_efficiency',
           'capital_adjusted_gross_profit', 'gppe', 'tier', 'target_ps_lower',
           'target_ps_upper', 'target_ps_midpoint', 'current_ps', 'valuation_gap',
           'confidence', 'freshness', 'reason_codes', 'input_observation_ids',
           'gppe_invocation_id', 'gppe_result_id',
           'gppe_definition_id', 'gppe_definition_sha256',
           'tier_definition_id', 'tier_definition_sha256'
       ])
       or (carries_pgp and not (new.payload ?& pgp_keys))
       or (not carries_pgp and (new.current_pgp is not null or new.target_pgp_lower is not null
           or new.target_pgp_upper is not null or new.target_pgp_midpoint is not null))
       or (carries_pgp and (
           (new.payload->>'current_pgp')::numeric is distinct from new.current_pgp
           or (new.payload->>'target_pgp_lower')::numeric is distinct from new.target_pgp_lower
           or (new.payload->>'target_pgp_upper')::numeric is distinct from new.target_pgp_upper
           or (new.payload->>'target_pgp_midpoint')::numeric is distinct from new.target_pgp_midpoint
       ))
       or new.payload->>'invocation_id' is distinct from new.invocation_id
       or new.payload->>'snapshot_id' is distinct from new.snapshot_id
       or new.payload->>'run_id' is distinct from new.run_id
       or new.payload->>'release_manifest_id' is distinct from new.release_manifest_id
       or new.payload->>'universe_id' is distinct from new.universe_id
       or new.payload->>'universe_version' is distinct from new.universe_version
       or new.payload->>'universe_sha256' is distinct from new.universe_sha256
       or (new.payload->>'cutoff')::timestamptz is distinct from new.cutoff
       or new.payload->>'issuer_id' is distinct from new.issuer_id
       or new.payload->>'instrument_id' is distinct from new.instrument_id
       or new.payload->>'listing_id' is distinct from new.listing_id
       or new.payload->>'operating_branch' is distinct from new.operating_branch
       or new.payload->>'operating_metric' is distinct from new.operating_metric
       or new.payload->>'availability' is distinct from new.availability
       or (new.payload->>'operating_efficiency')::numeric is distinct from new.operating_efficiency
       or (new.payload->>'capital_adjusted_gross_profit')::numeric
            is distinct from new.capital_adjusted_gross_profit
       or (new.payload->>'gppe')::numeric is distinct from new.gppe
       or new.payload->>'tier' is distinct from new.tier
       or (new.payload->>'target_ps_lower')::numeric is distinct from new.target_ps_lower
       or (new.payload->>'target_ps_upper')::numeric is distinct from new.target_ps_upper
       or (new.payload->>'target_ps_midpoint')::numeric is distinct from new.target_ps_midpoint
       or (new.payload->>'current_ps')::numeric is distinct from new.current_ps
       or (new.payload->>'valuation_gap')::numeric is distinct from new.valuation_gap
       or (new.payload->>'confidence')::numeric is distinct from new.confidence
       or new.payload->>'freshness' is distinct from new.freshness
       or array(select jsonb_array_elements_text(new.payload->'reason_codes'))
            is distinct from new.reason_codes
       or array(select jsonb_array_elements_text(new.payload->'input_observation_ids'))
            is distinct from new.input_observation_ids
       or new.payload->>'gppe_invocation_id' is distinct from new.gppe_invocation_id
       or new.payload->>'gppe_result_id' is distinct from new.gppe_result_id
       or new.payload->>'gppe_definition_id' is distinct from new.gppe_definition_id
       or new.payload->>'gppe_definition_sha256' is distinct from new.gppe_definition_sha256
       or new.payload->>'tier_definition_id' is distinct from new.tier_definition_id
       or new.payload->>'tier_definition_sha256' is distinct from new.tier_definition_sha256
       or raw.canonical_sha256(new.payload) <> new.content_sha256 then
        raise check_violation using message = 'TOPT core result does not match its invocation or content';
    end if;
    return new;
end;
$$;

comment on column mart.strategy_decisions.current_price_to_gross_profit is
    '#1235: market value / gross profit at the cutoff (4 dp). Set by definition 1.0.0 and later; NULL on a P/S definition and on an excluded decision.';
comment on column mart.strategy_decisions.target_price_to_gross_profit is
    '#1235: the tier band midpoint of target P/GP (4 dp). When set, valuation_gap is target P/GP / current P/GP - 1 and target_price_to_sales is NULL.';
comment on column mart.topt_core_results.current_pgp is
    '#1235: issuer market value / the GPPE operating numerator (gross profit, or a bank''s pre-provision profit).';
comment on column mart.topt_core_results.target_pgp_midpoint is
    '#1235: the P/GP band midpoint of the strategy definition named by tier_definition_id. valuation_gap = target_pgp_midpoint / current_pgp - 1.';
comment on column mart.fund_virtual_company.valuation_basis is
    '#1235: the multiple the weighted gap divides: price_to_gross_profit (strategy definition 1.0.0) or price_to_sales. NULL before #1235 and on a refused aggregate.';
comment on column mart.topt_core_results.target_ps_midpoint is
    'Before #1235: the P/S band midpoint. From #1235: the P/S that the P/GP band implies at the issuer''s gross margin (target_pgp_midpoint x numerator / revenue).';
