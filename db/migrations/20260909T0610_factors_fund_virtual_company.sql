-- #727 acceptance option 1 / #36 first slice (init.md §7 module 5): the ETF
-- virtual-company consolidation becomes a materialized factor output.
--
-- Before this, the fund-level weighted valuation gap was computed in the App's SQL
-- (`apps/app-web/src/server/mart/fund-valuation.ts`) with a window function, against that
-- file's own header and against init.md §1 rule 2 — the App layer may reformat within a
-- row, not jointly compute a metric across factors and rows. The number now comes from
-- `factors.base.etf_virtual_company`, written by the tick that produced the core
-- rows it consumed, and the App reads this table.
--
-- One row per (run_id, fund_id): the aggregate belongs to the governed run whose core
-- rows it weighted, so a later run never silently restates an earlier fund number.
create table if not exists mart.fund_virtual_company (
    run_id                    text not null,
    fund_id                   text not null,
    fund_name                 text not null default '',
    -- The holdings vintage the weights came from. Both are the FILING's times, not the
    -- run's: report_period is what the weights describe, transaction_time is when they
    -- became publicly knowable (#63's PIT split).
    report_period             date not null,
    transaction_time          timestamptz not null,
    cutoff                    timestamptz not null,
    definition_version        text not null check (definition_version <> ''),
    definition_sha256         text not null check (definition_sha256 ~ '^[0-9a-f]{64}$'),
    -- NULL when the consolidation was refused (coverage below the definition's floors);
    -- reason_codes says which floor. A refused aggregate is never a zero.
    weighted_valuation_gap    numeric,
    -- Percent of net assets, nested: valued <= resolved <= total. The reader never
    -- subtracts to learn what was dropped (#36: masses explicit and consistent).
    total_weight_pct          numeric not null,
    resolved_weight_pct       numeric not null,
    valued_weight_pct         numeric not null,
    lines                     integer not null check (lines >= 0),
    valued_lines              integer not null check (valued_lines >= 0),
    confidence                numeric not null check (confidence >= 0 and confidence <= 1),
    reason_codes              text[] not null default '{}',
    -- The three §8 status dimensions (#747), written by this producer like every other
    -- factor row. Vocabularies are `truealpha_contracts.execution`'s enums.
    availability_status       text not null
        check (availability_status in ('available', 'unavailable', 'stale', 'excluded', 'low_confidence', 'error')),
    source_evidence_status    text not null
        check (source_evidence_status in ('verified', 'degraded', 'rejected')),
    factor_validation_status  text not null
        check (factor_validation_status in ('accepted', 'rejected', 'not_evaluated')),
    created_at                timestamptz not null default clock_timestamp(),
    primary key (run_id, fund_id),
    -- The masses are nested by construction; a violation means the producer's join, not
    -- a policy choice, so the database refuses it rather than publishing an impossible row.
    check (valued_weight_pct <= resolved_weight_pct),
    check (resolved_weight_pct <= total_weight_pct),
    check (valued_lines <= lines)
);

create index if not exists ix_fund_virtual_company_fund
    on mart.fund_virtual_company (fund_id, cutoff desc);

comment on table mart.fund_virtual_company is
    '#36/#727 (init.md §7 module 5): one fund-level virtual-company row per governed run — the fund''s filed N-PORT weights consolidating that run''s core factor outputs, with the coverage mass the aggregate describes and the three §8 status dimensions.';
comment on column mart.fund_virtual_company.weighted_valuation_gap is
    'Weight-weighted mean valuation gap over the VALUED mass (denominator = valued_weight_pct); NULL when the definition''s coverage floors refused the aggregate — see reason_codes.';
comment on column mart.fund_virtual_company.definition_sha256 is
    'content_sha256 of the EtfConsolidationDefinition this row was computed under; two rows are comparable only under the same value.';
comment on column mart.fund_virtual_company.resolved_weight_pct is
    'Filed weight whose ISIN resolved to a listing. total - resolved is the unresolved (foreign/non-equity/uncrosswalked) mass.';
comment on column mart.fund_virtual_company.valued_weight_pct is
    'Resolved weight that also had an available core row on this run. resolved - valued is the identified-but-unvalued mass.';

-- The per-ISIN listing resolution over EVERY vintage (#706's rule, lifted out of
-- mart.fund_holdings_valuation). The valuation view pins the newest vintage, which is
-- right for a live page and wrong for a replay: a producer selecting the vintage knowable
-- at a historical cutoff needs the same resolution over the vintage IT chose (#36: "never
-- applies a later filing retroactively"). Both readers now build on one definition of
-- "which listing is this line", instead of the second copying the first's SQL.
create or replace view mart.fund_holdings_resolved as
with resolved as (
    select holdings.*,
           (
               select identifier.entity_id
               from staging.kg_identifiers identifier
               where identifier.identifier_type = 'isin'
                 and identifier.identifier_value = holdings.isin
               order by identifier.transaction_time desc, identifier.confidence desc, identifier.id desc
               limit 1
           ) as issuer_entity
    from mart.fund_holdings holdings
)
select resolved.fund_id,
       resolved.fund_name,
       resolved.report_period,
       resolved.transaction_time,
       resolved.holding_name,
       resolved.isin,
       resolved.percent_of_net_assets,
       resolved.value_usd,
       resolved.issuer_entity,
       coalesce(per_isin_ticker.identifier_value, issuer_ticker.identifier_value) as ticker,
       case
           when coalesce(per_isin_ticker.identifier_value, issuer_ticker.identifier_value) is not null
           then 'listing:xnas:' || lower(coalesce(per_isin_ticker.identifier_value, issuer_ticker.identifier_value))
       end as listing_id
from resolved
left join lateral (
    -- THIS ISIN's own listing (#706) — never another share class's.
    select identifier.identifier_value
    from staging.kg_identifiers identifier
    where identifier.identifier_type = 'ticker'
      and identifier.entity_id = 'company:isin:' || resolved.isin
    order by identifier.transaction_time desc, identifier.confidence desc, identifier.id desc
    limit 1
) per_isin_ticker on true
left join lateral (
    select identifier.identifier_value
    from staging.kg_identifiers identifier
    where identifier.identifier_type = 'ticker'
      and identifier.entity_id = resolved.issuer_entity
      and resolved.issuer_entity like 'issuer:cik:%'
    order by identifier.transaction_time desc, identifier.confidence desc, identifier.id desc
    limit 1
) issuer_ticker on true;

comment on view mart.fund_holdings_resolved is
    '#727: every fund-holding vintage with its per-ISIN listing resolution (#706). mart.fund_holdings_valuation is this view pinned to the newest vintage per fund; a PIT producer selects its own vintage here instead.';

-- Redefined on top of the shared resolution: same columns, same newest-per-fund
-- semantics, one copy of the resolution rule.
create or replace view mart.fund_holdings_valuation as
with newest as (
    select distinct on (fund_id) fund_id, report_period, transaction_time
    from mart.fund_holdings
    order by fund_id, report_period desc, transaction_time desc
)
select resolved.*
from mart.fund_holdings_resolved resolved
join newest using (fund_id, report_period, transaction_time);

-- The filed and resolved masses of each holdings vintage. These are properties of the
-- FILING and the identity graph, not of any valuation run: a fund that filed 99.5% of net
-- assets filed it whether or not a governed run has ever valued the fund. Kept out of
-- mart.fund_virtual_company's run-scoped row so a page with no governed run can still
-- report the filed mass instead of rendering 0.00% — a zero that would assert the fund
-- filed nothing (the repository's "a constant never stands in for a measurement" rule,
-- in its absent-row form).
create or replace view mart.fund_holdings_coverage as
select fund_id,
       report_period,
       transaction_time,
       count(*) as lines,
       count(*) filter (where listing_id is not null) as resolved_lines,
       coalesce(sum(percent_of_net_assets), 0) as total_weight_pct,
       coalesce(sum(percent_of_net_assets) filter (where listing_id is not null), 0) as resolved_weight_pct
from mart.fund_holdings_resolved
group by fund_id, report_period, transaction_time;

comment on view mart.fund_holdings_coverage is
    '#727: per holdings vintage, the filed and listing-resolved mass. Run-independent by design — the valued mass and the weighted aggregate are the module-5 factor''s, in mart.fund_virtual_company.';

-- Read roles: mart_readonly (the App's normal-user reader; roles.sql's blanket grant only
-- covers tables that existed when the role was created) and app_ops_reader (/admin/datahub).
-- Conditional because CI applies migrations before db/roles.sql creates the roles.
do $$
begin
    if exists (select from pg_roles where rolname = 'mart_readonly') then
        grant select on mart.fund_virtual_company to mart_readonly;
        grant select on mart.fund_holdings_resolved to mart_readonly;
        grant select on mart.fund_holdings_valuation to mart_readonly;
        grant select on mart.fund_holdings_coverage to mart_readonly;
    end if;
    if exists (select from pg_roles where rolname = 'app_ops_reader') then
        grant select on mart.fund_virtual_company to app_ops_reader;
    end if;
end;
$$;
