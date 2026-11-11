"""Validate materialized mart output invariants against SQL predicates (#581)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
from factors.panel import PANEL_METRICS, PUBLISHED_COLUMNS, IssuerClass, SignPolicy, ratio_operands_of
from truealpha_contracts import reconciliation_policies
from truealpha_contracts.environment_contract import EnvironmentContract, load_environment_contract
from truealpha_contracts.strategy import load_live_strategy_definition
from truealpha_contracts.universes import id_prefix_like, universe_with_role

EXEMPTIONS_PATH = Path(__file__).resolve().parents[4] / "tools" / "output_invariant_exemptions.json"
SERVED_UNIVERSE_LIKE = id_prefix_like(universe_with_role("strategy"))
_MAX_INPUT_AGE_DAYS = load_live_strategy_definition().eligibility.maximum_input_age_days

GOVERNED_HEAD = f"""
    select run_id from mart.served_head
    where factor_id = 'gross_profit_per_employee'
      and universe_id like '{SERVED_UNIVERSE_LIKE}'
    order by advanced_at desc limit 1
"""

DASHBOARD_STRATEGY = "large_model_value_v0"
LATEST_STRATEGY_RUN = f"""
    select strategy_run_id from (
        select r.strategy_run_id, r.executed_at, r.created_at,
               exists (select 1 from mart.governed_strategy_run g
                       where g.strategy_run_id = r.strategy_run_id) as is_governed
        from mart.strategy_runs r
        where r.strategy_key = '{DASHBOARD_STRATEGY}'
        order by is_governed desc, r.executed_at desc, r.created_at desc, r.strategy_run_id desc limit 1
    ) served
"""


def _expected_inputs() -> tuple[tuple[str, str], ...]:
    from factors.composite import strategy_evaluator
    from factors.panel import definition_pins

    definition = load_live_strategy_definition()
    peg_keys = set(strategy_evaluator._input_keys("peg"))
    return tuple(
        sorted(
            (key, issuer_class.value)
            for issuer_class in (IssuerClass(name) for name in definition_pins(definition))
            for key in strategy_evaluator.required_input_keys(definition, issuer_class) | peg_keys
        )
    )


def _primary_market_price_parser() -> str:
    try:
        from data_engine.datahub.production_topt.source_registrations import registration_for

        return registration_for("market-price").origins[0].parser_versions[-1].split(":")[0]
    except ImportError:
        return "production-topt-live-parser"


def _market_price_parser_ranks() -> tuple[tuple[str, int], ...]:
    try:
        from data_engine.datahub.production_topt.source_registrations import SOURCE_BY_PARSER, registration_for
    except ImportError:
        return ()
    market = {origin.origin_id for origin in registration_for("market-price").origins}
    priority = reconciliation_policies.RECONCILIATION_POLICY.source_priority
    ranks: dict[str, int] = {}
    for vintage, (source_id, origin_id, _value_key) in SOURCE_BY_PARSER.items():
        if origin_id in market and source_id in priority:
            family, rank = vintage.split(":")[0], priority.index(source_id)
            ranks[family] = min(rank, ranks.get(family, rank))
    return tuple(sorted(ranks.items(), key=lambda item: (item[1], item[0])))


EXPECTED_INPUTS = _expected_inputs()
PRIMARY_MARKET_PRICE_PARSER = _primary_market_price_parser()
MARKET_PRICE_PARSER_RANKS = _market_price_parser_ranks()


@dataclass(frozen=True)
class Invariant:
    id: str
    claim: str
    violations: str
    population: str
    signals: str | None = None


SIGN_POLICY_TABLE = "mart.topt_gppe_results"


def _classes(values: set[IssuerClass]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(member.value for member in values))


def _sign_policy_queries(table: str = SIGN_POLICY_TABLE) -> tuple[str, str | None]:
    columns = PUBLISHED_COLUMNS[table]
    judged: dict[str, str] = {}
    for column, node_key in sorted(columns.items()):
        judged.setdefault(node_key, column)
    violations: list[str] = []
    signals: list[str] = []

    def select(column: str, node_key: str, predicate: str, why: str, *, published: bool = True) -> str:
        node = PANEL_METRICS[node_key]
        scope = f" and {column} is not null" if published else ""
        return (
            f"select listing_id, operating_branch, '{column}' as published_column, "
            f"'{node.key}:{node.node_id}' as node, coalesce({column}::text, 'null') as value, '{why}' as policy "
            f"from {table} where run_id = ({GOVERNED_HEAD}){scope} and ({predicate})"
        )

    for column, node_key in sorted(columns.items()):
        first = judged[node_key]
        if column != first:
            violations.append(
                select(
                    column,
                    node_key,
                    f"{column} is distinct from {first}",
                    f"same node as {first}, other value",
                    published=False,
                )
            )
            continue
        node = PANEL_METRICS[node_key]
        by_policy = {
            policy: {c for c, declared in node.sign_policy.items() if declared is policy} for policy in SignPolicy
        }
        violations.append(
            select(
                column,
                node_key,
                f"operating_branch not in ({_classes(set(node.sign_policy))})",
                "no policy for this class",
            )
        )
        forbidden = by_policy[SignPolicy.MUST_BE_NON_NEGATIVE]
        if forbidden:
            violations.append(
                select(
                    column,
                    node_key,
                    f"{column} < 0 and operating_branch in ({_classes(forbidden)})",
                    SignPolicy.MUST_BE_NON_NEGATIVE.value,
                )
            )
        signalled = by_policy[SignPolicy.SIGN_IS_SIGNAL]
        if signalled:
            signals.append(
                select(
                    column,
                    node_key,
                    f"{column} < 0 and operating_branch in ({_classes(signalled)})",
                    SignPolicy.SIGN_IS_SIGNAL.value,
                )
            )
    for spec in PANEL_METRICS.values():
        if spec.key not in judged:
            continue
        pair = ratio_operands_of(spec)
        if pair is None:
            continue
        numerator, denominator = pair
        if numerator not in judged or denominator not in PANEL_METRICS:
            continue
        classes = {
            issuer_class
            for issuer_class in spec.applicability
            if PANEL_METRICS[denominator].sign_policy.get(issuer_class) is SignPolicy.MUST_BE_NON_NEGATIVE
        }
        if classes:
            output_column, numerator_column = judged[spec.key], judged[numerator]
            violations.append(
                select(
                    output_column,
                    spec.key,
                    f"operating_branch in ({_classes(classes)}) and ({numerator_column} is null "
                    f"or sign({output_column}) <> sign({numerator_column}))",
                    f"sign of {numerator_column} over non-negative {denominator}",
                )
            )
    return "\n union all\n".join(violations), ("\n union all\n".join(signals) if signals else None)


_SIGN_VIOLATIONS, _SIGN_SIGNALS = _sign_policy_queries()

_FUSION_CTES = """
    selected as (
        select s.run_id, sel.observation_id
        from mart.current_pointer_head head
        join staging.topt_core_snapshots s
          on s.run_id = head.target_run_id
         and s.universe_id = head.universe_id
         and s.universe_version = head.universe_version
        cross join lateral jsonb_array_elements(s.payload->'members') member
        cross join lateral jsonb_array_elements_text(member->'observation_ids') sel(observation_id)
    ), contested as (
        select uo.capture_obligation_id
        from staging.capture_observation_obligations uo
        join staging.capture_normalized_observations o on o.observation_id = uo.observation_id
        group by uo.capture_obligation_id
        having count(distinct split_part(o.parser_version, ':', 1)) > 1
    )
"""

_SCHEDULED_HEADS_CTE = """
    with heads as (
        select h.universe_id, coalesce(h.run_id, h.head_run_id) as run_id
        from mart.served_head h
        where h.factor_id = 'gross_profit_per_employee'
          and __HEAD_IS_SCHEDULED__
    )
"""

_TEMPLATES: tuple[Invariant, ...] = (
    Invariant(
        id="node-sign-policy",
        claim=(
            "a published value respects the sign policy its metric-forest node declares for the "
            "issuer's class — the definition says what a negative number means, and this asserts "
            "exactly that (init.md rule 17, #528)"
        ),
        violations=_SIGN_VIOLATIONS,
        population=f"select count(*) from {SIGN_POLICY_TABLE} where run_id = ({GOVERNED_HEAD})",
        signals=_SIGN_SIGNALS,
    ),
    Invariant(
        id="available-means-a-value",
        claim=(
            "a cell marked `available` carries a value and a non-zero confidence — "
            "'available with nothing in it' is a contradiction, not a state"
        ),
        violations=f"""
            select listing_id, availability, coalesce(gppe::text, 'null') as gppe, confidence::text
            from mart.topt_gppe_results
            where run_id = ({GOVERNED_HEAD})
              and availability = 'available'
              and (gppe is null or confidence = 0)
        """,
        population=f"""
            select count(*) from mart.topt_gppe_results
            where run_id = ({GOVERNED_HEAD}) and availability = 'available'
        """,
    ),
    Invariant(
        id="selected-weights-close",
        claim="the selected positions of a strategy run sum to a whole portfolio",
        violations=f"""
            select strategy_run_id, round(sum(target_weight), 6)::text as total, count(*)::text as selected
            from mart.strategy_decisions
            where strategy_run_id = ({LATEST_STRATEGY_RUN}) and outcome = 'selected'
            group by strategy_run_id
            having abs(sum(target_weight) - 1) > 0.000001
        """,
        population=f"""
            select count(*) from mart.strategy_decisions
            where strategy_run_id = ({LATEST_STRATEGY_RUN}) and outcome = 'selected'
        """,
    ),
    Invariant(
        id="every-consumed-input-is-populated",
        claim=(
            "an input key the strategy consumes must be written for at least one issuer at "
            "the latest cutoff — a key that is declared, permitted by the 0039 CHECK and "
            "written for NOBODY is a wiring gap, and it publishes as a missing factor rather "
            "than an error (#284)"
        ),
        violations="""
            with expected(input_key, issuer_class) as (values __EXPECTED_VALUES__
            ), latest as (
                select max(cutoff_at) as cutoff_at from staging.strategy_backtest_inputs
            ), present as (
                select distinct r.operating_branch as issuer_class from mart.topt_core_results r
                where r.cutoff = (select cutoff_at from latest)
            )
            select distinct expected.input_key, (select cutoff_at from latest)::text as cutoff_at
            from expected
            where exists (select 1 from latest where cutoff_at is not null)
              and (expected.issuer_class in (select issuer_class from present)
                   or not exists (select 1 from present))
              and not exists (
                select 1 from staging.strategy_backtest_inputs i
                where i.input_key = expected.input_key
                  and i.cutoff_at = (select cutoff_at from latest)
              )
        """,
        population="""
            select count(distinct input_key) from staging.strategy_backtest_inputs
            where cutoff_at = (select max(cutoff_at) from staging.strategy_backtest_inputs)
        """,
    ),
    Invariant(
        id="fusion-selects-by-priority-not-recency",
        claim=(
            "where two sources assert the same obligation, the snapshot selected the "
            "highest-priority source -- never whichever published later (init.md rule 12)"
        ),
        violations=f"""
            with ranks(family, rank) as (values __MARKET_PRICE_PARSER_RANKS__),
            {_FUSION_CTES}
            select selected.run_id, o.semantic_type, split_part(o.parser_version, ':', 1) as selected_parser
            from selected
            join staging.capture_normalized_observations o on o.observation_id = selected.observation_id
            join staging.capture_observation_payloads p on p.observation_id = o.observation_id
            join staging.capture_observation_obligations uo on uo.observation_id = o.observation_id
            join contested on contested.capture_obligation_id = uo.capture_obligation_id
            left join ranks selected_rank on selected_rank.family = split_part(o.parser_version, ':', 1)
            where o.semantic_type = 'market-price'
              and split_part(o.parser_version, ':', 1) <> '{{PRIMARY_MARKET_PRICE_PARSER}}'
              and (
                  selected_rank.rank is null
                  or p.normalized_payload ->> 'served_by_failover' is null
                  or exists (
                      select 1
                      from staging.capture_observation_obligations peer_link
                      join staging.capture_normalized_observations peer
                        on peer.observation_id = peer_link.observation_id
                      join ranks peer_rank on peer_rank.family = split_part(peer.parser_version, ':', 1)
                      where peer_link.capture_obligation_id = uo.capture_obligation_id
                        and peer_rank.rank < selected_rank.rank
                        and (peer.knowable_at at time zone 'UTC')::date = (o.knowable_at at time zone 'UTC')::date
                  )
              )
        """,
        population=f"""
            with {_FUSION_CTES}
            select count(*)
            from selected
            join staging.capture_observation_obligations uo on uo.observation_id = selected.observation_id
            join contested on contested.capture_obligation_id = uo.capture_obligation_id
        """,
    ),
    Invariant(
        id="extraction-citations-resolve",
        claim=(
            "every issuer headcount fact must carry an 18-digit SEC accession and a filing knowable_at, "
            "and a 10k-extraction citation must resolve to captured bytes in raw.fetches (#1295, #735, #70)"
        ),
        violations="""
            with eligible as (
                select distinct g.cik
                from staging.issuer_headcount_facts g
                where g.source in ('10k-extraction', 'manual-review', 'manual-reviewed', 'sec-10k-regex-v1')
                  and (
                      substring(g.evidence_ref from 'accession=([0-9]{18}|[0-9]{10}-[0-9]{2}-[0-9]{6})') is not null
                      or (
                          g.source = '10k-extraction'
                          and substring(g.evidence_ref from 'accession=([0-9]{4}-[0-9]{2}-[0-9]{6})') is not null
                      )
                  )
                  and substring(g.evidence_ref from 'filed=([0-9]{4}-[0-9]{2}-[0-9]{2})') is not null
                  and g.knowable_at <> '2026-01-01T00:00:00Z'::timestamptz
                  and (g.knowable_at at time zone 'UTC')::date
                      = substring(g.evidence_ref from 'filed=([0-9]{4}-[0-9]{2}-[0-9]{2})')::date
                  and (
                      g.source <> '10k-extraction'
                      or (
                          substring(g.evidence_ref from 'raw=raw\\.fetches:([0-9]+)') is not null
                          and exists (
                              select 1 from raw.fetches r
                              where r.id = substring(g.evidence_ref from 'raw=raw\\.fetches:([0-9]+)')::bigint
                          )
                      )
                  )
            )
            select
                f.id::text,
                f.cik::text,
                f.source,
                coalesce(
                    substring(f.evidence_ref from 'accession=([0-9]{18}|[0-9]{10}-[0-9]{2}-[0-9]{6}|[0-9]{4}-[0-9]{2}-[0-9]{6})'),
                    'missing-accession'
                ) as cited_accession,
                coalesce(substring(f.evidence_ref from 'raw=raw\\.fetches:([0-9]+)'), 'none') as cited_raw_id
            from staging.issuer_headcount_facts f
            where (
                f.source = 'curated-10k-2025'
                or (
                    substring(f.evidence_ref from 'accession=([0-9]{18}|[0-9]{10}-[0-9]{2}-[0-9]{6})') is null
                    and not (
                        f.source = '10k-extraction'
                        and substring(f.evidence_ref from 'accession=([0-9]{4}-[0-9]{2}-[0-9]{6})') is not null
                    )
                )
                or substring(f.evidence_ref from 'filed=([0-9]{4}-[0-9]{2}-[0-9]{2})') is null
                or f.knowable_at = '2026-01-01T00:00:00Z'::timestamptz
                or (f.knowable_at at time zone 'UTC')::date
                   <> substring(f.evidence_ref from 'filed=([0-9]{4}-[0-9]{2}-[0-9]{2})')::date
                or (
                    f.source = '10k-extraction'
                    and (
                        substring(f.evidence_ref from 'raw=raw\\.fetches:([0-9]+)') is null
                        or not exists (
                            select 1 from raw.fetches r
                            where r.id = substring(f.evidence_ref from 'raw=raw\\.fetches:([0-9]+)')::bigint
                        )
                    )
                )
            )
            and not (
                (
                    f.source = 'curated-10k-2025'
                    or f.knowable_at = '2026-01-01T00:00:00Z'::timestamptz
                    or f.evidence_ref like '%#70 supersedes%'
                )
                and exists (select 1 from eligible e where e.cik = f.cik)
            )
        """,
        population="select count(*) from staging.issuer_headcount_facts",
    ),
    Invariant(
        id="revenue-period-is-recent",
        claim=(
            f"each available core result of a scheduled governed head has a revenue period that ends at most "
            f"{_MAX_INPUT_AGE_DAYS} days before the cutoff of its run, and each such head states a revenue "
            "period in at least one of its results — an older period is a stale figure that the head serves as "
            "a current one, and a head with no stated period has an unknown age (#1457, #1519)"
        ),
        violations=f"""
            {_SCHEDULED_HEADS_CTE},
            served as (
                select distinct heads.universe_id, r.run_id, r.issuer_id, r.revenue_period_end,
                       (r.cutoff at time zone 'UTC')::date as cutoff
                from heads
                join mart.topt_core_results r on r.run_id = heads.run_id
                where r.availability = 'available'
            )
            select universe_id, run_id, issuer_id, revenue_period_end::text, cutoff::text
            from served
            where revenue_period_end < (cutoff - interval '{_MAX_INPUT_AGE_DAYS} days')::date
            union all
            select distinct universe_id, run_id, 'no-result-states-a-revenue-period', '-', '-'
            from served head_rows
            where not exists (
                select 1 from served dated
                where dated.run_id = head_rows.run_id and dated.revenue_period_end is not null
            )
            order by 1, 2, 3
        """,
        population="""
            select count(distinct (r.run_id, r.issuer_id))
            from mart.served_head h
            join mart.topt_core_results r on r.run_id = coalesce(h.run_id, h.head_run_id)
            where h.factor_id = 'gross_profit_per_employee'
              and __HEAD_IS_SCHEDULED__
              and r.availability = 'available'
              and r.revenue_period_end is not null
        """,
    ),
    Invariant(
        id="governed-entities-resolve-display-name",
        claim=(
            "every issuer in an active governed head resolves to a human-readable company name in "
            "mart.entity_display_resolution, never NULL, its raw issuer_id, or its bare ticker (#1517)"
        ),
        violations=f"""
            {_SCHEDULED_HEADS_CTE},
            members as (
                select distinct heads.universe_id, r.run_id, r.issuer_id
                from heads
                join mart.topt_core_results r on r.run_id = heads.run_id
            )
            select m.universe_id, m.run_id, m.issuer_id, coalesce(d.display_name, 'NULL')
            from members m
            left join mart.entity_display_resolution d on d.issuer_id = m.issuer_id
            where nullif(btrim(d.display_name), '') is null
               or lower(btrim(d.display_name)) = lower(btrim(m.issuer_id))
               or lower(btrim(d.display_name)) = lower(btrim(coalesce(d.ticker, '')))
            order by 1, 2, 3
        """,
        population="""
            select count(distinct (r.run_id, r.issuer_id))
            from mart.served_head h
            join mart.topt_core_results r on r.run_id = coalesce(h.run_id, h.head_run_id)
            where h.factor_id = 'gross_profit_per_employee'
              and __HEAD_IS_SCHEDULED__
        """,
    ),
    Invariant(
        id="entity-identity-unique-per-entity",
        claim="mart.entity_identity returns at most one row per entity_id — duplicate rows fan out every downstream join (#1641)",
        violations="""
            select entity_id::text, count(*)::text as row_count
            from mart.entity_identity
            group by entity_id
            having count(*) > 1
            order by 1
        """,
        population="select count(*) from mart.entity_identity",
    ),
    Invariant(
        id="fund-virtual-company-weights-conserved",
        claim="every mart.fund_virtual_company row conserves portfolio weight (0 <= valued_weight_pct <= resolved_weight_pct <= total_weight_pct <= 105) (#1641)",
        violations="""
            select run_id, fund_id, total_weight_pct::text, resolved_weight_pct::text, valued_weight_pct::text
            from mart.fund_virtual_company
            where total_weight_pct < 0
               or total_weight_pct > 105
               or resolved_weight_pct < 0
               or resolved_weight_pct > total_weight_pct
               or valued_weight_pct < 0
               or valued_weight_pct > resolved_weight_pct
            order by 1, 2
        """,
        population="select count(*) from mart.fund_virtual_company",
    ),
)


def _rank_values(ranks: tuple[tuple[str, int], ...]) -> str:
    return ", ".join(f"('{family}', {rank})" for family, rank in ranks) or "(null::text, null::integer)"


def head_is_scheduled_sql(contract: EnvironmentContract) -> str:
    unscheduled = []
    for environment, prefixes in contract.unscheduled_id_prefixes().items():
        if not prefixes:
            continue
        names = ", ".join(f"'{name}'" for name in contract.environment_names(environment))
        likes = ", ".join(f"'{prefix}%'" for prefix in prefixes)
        unscheduled.append(
            f"(lower(trim(coalesce(h.environment, ''))) in ({names})"
            f" and coalesce(h.universe_id, '') like any (array[{likes}]))"
        )
    return f"not ({' or '.join(unscheduled)})" if unscheduled else "true"


def _render(invariant: Invariant, scheduled: str) -> Invariant:
    values = ", ".join(f"('{key}', '{issuer_class}')" for key, issuer_class in EXPECTED_INPUTS)
    ranks = _rank_values(MARKET_PRICE_PARSER_RANKS)

    def fill(sql: str) -> str:
        return (
            sql.replace("__EXPECTED_VALUES__", values)
            .replace("{PRIMARY_MARKET_PRICE_PARSER}", PRIMARY_MARKET_PRICE_PARSER)
            .replace("__MARKET_PRICE_PARSER_RANKS__", ranks)
            .replace("__HEAD_IS_SCHEDULED__", scheduled)
        )

    return Invariant(
        id=invariant.id,
        claim=invariant.claim,
        violations=fill(invariant.violations),
        population=fill(invariant.population),
        signals=None if invariant.signals is None else fill(invariant.signals),
    )


def render_invariants(contract: EnvironmentContract) -> tuple[Invariant, ...]:
    scheduled = head_is_scheduled_sql(contract)
    return tuple(_render(template, scheduled) for template in _TEMPLATES)


INVARIANTS: tuple[Invariant, ...] = render_invariants(load_environment_contract())


@dataclass(frozen=True)
class Exemption:
    invariant: str
    issue: str
    expires: date
    reason: str


def load_exemptions(path: Path = EXEMPTIONS_PATH) -> dict[str, Exemption]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        entry["invariant"]: Exemption(
            invariant=entry["invariant"],
            issue=entry["issue"],
            expires=date.fromisoformat(entry["expires"]),
            reason=entry["reason"],
        )
        for entry in raw["exemptions"]
    }


def _rows(connection: Any, sql: str) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


def check(
    database_url: str,
    *,
    require_coverage: bool = False,
    today: date | None = None,
    exemptions: dict[str, Exemption] | None = None,
    invariants: Sequence[Invariant] = INVARIANTS,
    connect: Callable[[str], Any] = psycopg.connect,
) -> int:
    today = today or date.today()
    exemptions = load_exemptions() if exemptions is None else exemptions
    failures: list[str] = []

    with connect(database_url) as connection:
        for invariant in invariants:
            population = _rows(connection, invariant.population)[0][0]
            violations = _rows(connection, invariant.violations)
            exemption = exemptions.get(invariant.id)
            if invariant.signals is not None:
                for row in _rows(connection, invariant.signals):
                    print(f"  SIGNAL   {invariant.id}: " + " ".join(str(field) for field in row))

            if violations and exemption and exemption.expires >= today:
                print(
                    f"  DEFERRED {invariant.id}: {len(violations)} violation(s), exempt until "
                    f"{exemption.expires} under {exemption.issue} — {exemption.reason}"
                )
                continue
            if violations:
                detail = "; ".join(" ".join(str(field) for field in row) for row in violations[:5])
                expired = f" The exemption under {exemption.issue} expired {exemption.expires}." if exemption else ""
                failures.append(
                    f"{invariant.id}: {len(violations)} violation(s) — {invariant.claim}."
                    f"{expired} Offending rows: {detail}"
                )
                continue
            if population == 0:
                print(f"  EMPTY    {invariant.id}: examined 0 rows, so it proved nothing")
                if require_coverage:
                    failures.append(f"{invariant.id}: examined 0 rows where real data was required")
                continue
            if exemption:
                failures.append(
                    f"{invariant.id}: exempt under {exemption.issue} but no longer violated across "
                    f"{population} row(s). Remove the exemption — a standing waiver for a fixed "
                    f"defect hides the next one"
                )
                continue
            print(f"  ok       {invariant.id}: {population} row(s) examined")

    for failure in failures:
        print(f"invariant failed: {failure}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--require-coverage", action="store_true")
    args = parser.parse_args(argv)
    return check(args.database_url, require_coverage=args.require_coverage)


if __name__ == "__main__":
    raise SystemExit(main())
