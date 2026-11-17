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
from factors.panel import PANEL_METRICS, PUBLISHED_COLUMNS, IssuerClass, SignPolicy, definition_pins, ratio_operands_of
from truealpha_contracts import reconciliation_policies
from truealpha_contracts.environment_contract import EnvironmentContract, load_environment_contract
from truealpha_contracts.strategy import load_live_strategy_definition
from truealpha_contracts.universes import id_prefix_like, universe_with_role

EXEMPTIONS_PATH = Path(__file__).resolve().parents[4] / "tools" / "output_invariant_exemptions.json"
SERVED_UNIVERSE_LIKE = id_prefix_like(universe_with_role("strategy"))
_MAX_INPUT_AGE_DAYS = load_live_strategy_definition().eligibility.maximum_input_age_days

GOVERNED_HEAD = (
    "select run_id from mart.served_head where factor_id = 'gross_profit_per_employee' "
    f"and universe_id like '{SERVED_UNIVERSE_LIKE}' order by advanced_at desc limit 1"
)

DASHBOARD_STRATEGY = "large_model_value_v0"
LATEST_STRATEGY_RUN = (
    "select strategy_run_id from (select r.strategy_run_id, r.executed_at, r.created_at, "
    "exists (select 1 from mart.governed_strategy_run g where g.strategy_run_id = r.strategy_run_id) as is_governed "
    f"from mart.strategy_runs r where r.strategy_key = '{DASHBOARD_STRATEGY}' "
    "order by is_governed desc, r.executed_at desc, r.created_at desc, r.strategy_run_id desc limit 1) served"
)


def _expected_inputs() -> tuple[tuple[str, str], ...]:
    from factors.composite import strategy_evaluator

    definition = load_live_strategy_definition()
    peg_keys = set(strategy_evaluator._input_keys("peg"))
    return tuple(
        sorted(
            (key, issuer_class.value)
            for issuer_class in (IssuerClass(name) for name in definition_pins(definition))
            for key in strategy_evaluator.required_input_keys(definition, issuer_class) | peg_keys
        )
    )


def _market_price_config() -> tuple[str, tuple[tuple[str, int], ...]]:
    try:
        from data_engine.datahub.production_topt.source_registrations import SOURCE_BY_PARSER, registration_for
    except ImportError:
        return "production-topt-live-parser", ()
    origins = registration_for("market-price").origins
    primary = origins[0].parser_versions[-1].split(":")[0]
    market = {origin.origin_id for origin in origins}
    priority = reconciliation_policies.RECONCILIATION_POLICY.source_priority
    ranks: dict[str, int] = {}
    for vintage, (source_id, origin_id, _value_key) in SOURCE_BY_PARSER.items():
        if origin_id in market and source_id in priority:
            family, rank = vintage.split(":")[0], priority.index(source_id)
            ranks[family] = min(rank, ranks.get(family, rank))
    return primary, tuple(sorted(ranks.items(), key=lambda item: (item[1], item[0])))


def _primary_market_price_parser() -> str:
    return _market_price_config()[0]


def _market_price_parser_ranks() -> tuple[tuple[str, int], ...]:
    return _market_price_config()[1]


EXPECTED_INPUTS = _expected_inputs()
PRIMARY_MARKET_PRICE_PARSER, MARKET_PRICE_PARSER_RANKS = _market_price_config()


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


def _sign_policy_queries() -> tuple[str, str | None]:
    table = SIGN_POLICY_TABLE
    ordered_cols = sorted(PUBLISHED_COLUMNS[table].items())
    judged: dict[str, str] = {}
    for column, node_key in ordered_cols:
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

    for column, node_key in ordered_cols:
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
        violations.append(
            select(
                column,
                node_key,
                f"operating_branch not in ({_classes(set(node.sign_policy))})",
                "no policy for this class",
            )
        )
        for policy, target in ((SignPolicy.MUST_BE_NON_NEGATIVE, violations), (SignPolicy.SIGN_IS_SIGNAL, signals)):
            if matched := {c for c, declared in node.sign_policy.items() if declared is policy}:
                target.append(
                    select(
                        column, node_key, f"{column} < 0 and operating_branch in ({_classes(matched)})", policy.value
                    )
                )
    for spec in PANEL_METRICS.values():
        if spec.key not in judged or (pair := ratio_operands_of(spec)) is None:
            continue
        numerator, denominator = pair
        if numerator not in judged or denominator not in PANEL_METRICS:
            continue
        if classes := {
            cls
            for cls in spec.applicability
            if PANEL_METRICS[denominator].sign_policy.get(cls) is SignPolicy.MUST_BE_NON_NEGATIVE
        }:
            out_col, num_col = judged[spec.key], judged[numerator]
            violations.append(
                select(
                    out_col,
                    spec.key,
                    f"operating_branch in ({_classes(classes)}) and ({num_col} is null or sign({out_col}) <> sign({num_col}))",
                    f"sign of {num_col} over non-negative {denominator}",
                )
            )
    return "\n union all\n".join(violations), ("\n union all\n".join(signals) if signals else None)


_SIGN_VIOLATIONS, _SIGN_SIGNALS = _sign_policy_queries()

_FUSION_CTES = (
    "selected as ("
    "select s.run_id, sel.observation_id from mart.current_pointer_head head "
    "join staging.topt_core_snapshots s on s.run_id = head.target_run_id "
    "and s.universe_id = head.universe_id and s.universe_version = head.universe_version "
    "cross join lateral jsonb_array_elements(s.payload->'members') member "
    "cross join lateral jsonb_array_elements_text(member->'observation_ids') sel(observation_id)"
    "), contested as ("
    "select uo.capture_obligation_id from staging.capture_observation_obligations uo "
    "join staging.capture_normalized_observations o on o.observation_id = uo.observation_id "
    "group by uo.capture_obligation_id having count(distinct split_part(o.parser_version, ':', 1)) > 1"
    ")"
)

_FUSION_JOIN = (
    "from selected "
    "join staging.capture_observation_obligations uo on uo.observation_id = selected.observation_id "
    "join contested on contested.capture_obligation_id = uo.capture_obligation_id"
)
_SCHEDULED_HEADS_CTE = (
    "with heads as (select h.universe_id, coalesce(h.run_id, h.head_run_id) as run_id "
    "from mart.served_head h where h.factor_id = 'gross_profit_per_employee' and __HEAD_IS_SCHEDULED__)"
)
_SCHEDULED_RESULTS_POP = (
    "select count(distinct (r.run_id, r.issuer_id)) from mart.served_head h "
    "join mart.topt_core_results r on r.run_id = coalesce(h.run_id, h.head_run_id) "
    "where h.factor_id = 'gross_profit_per_employee' and __HEAD_IS_SCHEDULED__"
)
_ACC18_RE = "accession=([0-9]{18}|[0-9]{10}-[0-9]{2}-[0-9]{6})"
_ACC10_RE = "accession=([0-9]{4}-[0-9]{2}-[0-9]{6})"
_ACC_ANY_RE = "accession=([0-9]{18}|[0-9]{10}-[0-9]{2}-[0-9]{6}|[0-9]{4}-[0-9]{2}-[0-9]{6})"
_FILED_RE = "filed=([0-9]{4}-[0-9]{2}-[0-9]{2})"
_RAW_RE = r"raw=raw\.fetches:([0-9]+)"
_ZERO_TS = "'2026-01-01T00:00:00Z'::timestamptz"


def _valid_headcount_citation(alias: str) -> str:
    filed = f"substring({alias}.evidence_ref from '{_FILED_RE}')"
    raw = f"substring({alias}.evidence_ref from '{_RAW_RE}')"
    return (
        f"(substring({alias}.evidence_ref from '{_ACC18_RE}') is not null "
        f"or ({alias}.source = '10k-extraction' and substring({alias}.evidence_ref from '{_ACC10_RE}') is not null)) "
        f"and {filed} is not null and {alias}.knowable_at <> {_ZERO_TS} "
        f"and ({alias}.knowable_at at time zone 'UTC')::date = {filed}::date "
        f"and ({alias}.source <> '10k-extraction' or ({raw} is not null "
        f"and exists (select 1 from raw.fetches r where r.id = {raw}::bigint)))"
    )


_AVAILABLE_GPPE_FROM = f"from mart.topt_gppe_results where run_id = ({GOVERNED_HEAD}) and availability = 'available'"
_SELECTED_DECISIONS_FROM = (
    f"from mart.strategy_decisions where strategy_run_id = ({LATEST_STRATEGY_RUN}) and outcome = 'selected'"
)

_TEMPLATES: tuple[Invariant, ...] = (
    Invariant(
        "node-sign-policy",
        "published values respect metric-forest per-class sign policies (init.md rule 17, #528)",
        _SIGN_VIOLATIONS,
        f"select count(*) from {SIGN_POLICY_TABLE} where run_id = ({GOVERNED_HEAD})",
        _SIGN_SIGNALS,
    ),
    Invariant(
        "available-means-a-value",
        "available cells carry a value and non-zero confidence",
        f"select listing_id, availability, coalesce(gppe::text, 'null') as gppe, confidence::text {_AVAILABLE_GPPE_FROM} and (gppe is null or confidence = 0)",
        f"select count(*) {_AVAILABLE_GPPE_FROM}",
    ),
    Invariant(
        "selected-weights-close",
        "selected positions sum to 1",
        f"select strategy_run_id, round(sum(target_weight), 6)::text as total, count(*)::text as selected {_SELECTED_DECISIONS_FROM} group by strategy_run_id having abs(sum(target_weight) - 1) > 0.000001",
        f"select count(*) {_SELECTED_DECISIONS_FROM}",
    ),
    Invariant(
        "every-consumed-input-is-populated",
        "every consumed strategy input is populated at latest cutoff (#284)",
        (
            "with expected(input_key, issuer_class) as (values __EXPECTED_VALUES__), "
            "latest as (select max(cutoff_at) as cutoff_at from staging.strategy_backtest_inputs), "
            "present as (select distinct r.operating_branch as issuer_class from mart.topt_core_results r "
            "where r.cutoff = (select cutoff_at from latest)) "
            "select distinct expected.input_key, (select cutoff_at from latest)::text as cutoff_at from expected "
            "where exists (select 1 from latest where cutoff_at is not null) "
            "and (expected.issuer_class in (select issuer_class from present) or not exists (select 1 from present)) "
            "and not exists (select 1 from staging.strategy_backtest_inputs i "
            "where i.input_key = expected.input_key and i.cutoff_at = (select cutoff_at from latest))"
        ),
        (
            "select count(distinct input_key) from staging.strategy_backtest_inputs "
            "where cutoff_at = (select max(cutoff_at) from staging.strategy_backtest_inputs)"
        ),
    ),
    Invariant(
        "fusion-selects-by-priority-not-recency",
        "contested obligations pick highest-priority source, not latest publisher (init.md rule 12)",
        f"with ranks(family, rank) as (values __MARKET_PRICE_PARSER_RANKS__), {_FUSION_CTES} "
        "select selected.run_id, o.semantic_type, split_part(o.parser_version, ':', 1) as selected_parser "
        f"{_FUSION_JOIN} "
        "join staging.capture_normalized_observations o on o.observation_id = selected.observation_id "
        "join staging.capture_observation_payloads p on p.observation_id = o.observation_id "
        "left join ranks selected_rank on selected_rank.family = split_part(o.parser_version, ':', 1) "
        "where o.semantic_type = 'market-price' and split_part(o.parser_version, ':', 1) <> '{PRIMARY_MARKET_PRICE_PARSER}' "
        "and (selected_rank.rank is null or p.normalized_payload ->> 'served_by_failover' is null "
        "or exists (select 1 from staging.capture_observation_obligations peer_link "
        "join staging.capture_normalized_observations peer on peer.observation_id = peer_link.observation_id "
        "join ranks peer_rank on peer_rank.family = split_part(peer.parser_version, ':', 1) "
        "where peer_link.capture_obligation_id = uo.capture_obligation_id and peer_rank.rank < selected_rank.rank "
        "and (peer.knowable_at at time zone 'UTC')::date = (o.knowable_at at time zone 'UTC')::date))",
        f"with {_FUSION_CTES} select count(*) {_FUSION_JOIN}",
    ),
    Invariant(
        "extraction-citations-resolve",
        "headcount facts cite SEC accession and filing knowable_at; 10k-extraction resolves in raw.fetches (#1295, #735, #70)",
        (
            "with eligible as (select distinct g.cik from staging.issuer_headcount_facts g "
            "where g.source in ('10k-extraction', 'manual-review', 'manual-reviewed', 'sec-10k-regex-v1') "
            f"and {_valid_headcount_citation('g')}) "
            "select f.id::text, f.cik::text, f.source, "
            f"coalesce(substring(f.evidence_ref from '{_ACC_ANY_RE}'), 'missing-accession') as cited_accession, "
            f"coalesce(substring(f.evidence_ref from '{_RAW_RE}'), 'none') as cited_raw_id "
            "from staging.issuer_headcount_facts f "
            f"where (f.source = 'curated-10k-2025' or not ({_valid_headcount_citation('f')})) "
            f"and not ((f.source = 'curated-10k-2025' or f.knowable_at = {_ZERO_TS} or f.evidence_ref like '%#70 supersedes%') "
            "and exists (select 1 from eligible e where e.cik = f.cik))"
        ),
        "select count(*) from staging.issuer_headcount_facts",
    ),
    Invariant(
        "revenue-period-is-recent",
        f"available scheduled-head core results have revenue_period_end within {_MAX_INPUT_AGE_DAYS}d of cutoff and >= 1 stated period (#1457, #1519)",
        f"{_SCHEDULED_HEADS_CTE}, served as ("
        "select distinct heads.universe_id, r.run_id, r.issuer_id, r.revenue_period_end, "
        "(r.cutoff at time zone 'UTC')::date as cutoff from heads "
        "join mart.topt_core_results r on r.run_id = heads.run_id where r.availability = 'available') "
        f"select universe_id, run_id, issuer_id, revenue_period_end::text, cutoff::text from served "
        f"where revenue_period_end < (cutoff - interval '{_MAX_INPUT_AGE_DAYS} days')::date "
        "union all select distinct universe_id, run_id, 'no-result-states-a-revenue-period', '-', '-' from served head_rows "
        "where not exists (select 1 from served dated where dated.run_id = head_rows.run_id and dated.revenue_period_end is not null) "
        "order by 1, 2, 3",
        f"{_SCHEDULED_RESULTS_POP} and r.availability = 'available' and r.revenue_period_end is not null",
    ),
    Invariant(
        "governed-entities-resolve-display-name",
        "active governed-head issuers resolve to non-trivial display_name in mart.entity_display_resolution (#1517)",
        f"{_SCHEDULED_HEADS_CTE}, members as ("
        "select distinct heads.universe_id, r.run_id, r.issuer_id from heads "
        "join mart.topt_core_results r on r.run_id = heads.run_id) "
        "select m.universe_id, m.run_id, m.issuer_id, coalesce(d.display_name, 'NULL') from members m "
        "left join mart.entity_display_resolution d on d.issuer_id = m.issuer_id "
        "where nullif(btrim(d.display_name), '') is null "
        "or lower(btrim(d.display_name)) = lower(btrim(m.issuer_id)) "
        "or lower(btrim(d.display_name)) = lower(btrim(coalesce(d.ticker, ''))) order by 1, 2, 3",
        _SCHEDULED_RESULTS_POP,
    ),
    Invariant(
        "entity-identity-unique-per-entity",
        "mart.entity_identity has <= 1 row per entity_id (#1641)",
        "select entity_id::text, count(*)::text as row_count from mart.entity_identity group by entity_id having count(*) > 1 order by 1",
        "select count(*) from mart.entity_identity",
    ),
    Invariant(
        "fund-virtual-company-weights-conserved",
        "mart.fund_virtual_company satisfies 0 <= valued_weight_pct <= resolved_weight_pct <= total_weight_pct <= 105 (#1641)",
        (
            "select run_id, fund_id, total_weight_pct::text, resolved_weight_pct::text, valued_weight_pct::text "
            "from mart.fund_virtual_company where total_weight_pct < 0 or total_weight_pct > 105 "
            "or resolved_weight_pct < 0 or resolved_weight_pct > total_weight_pct "
            "or valued_weight_pct < 0 or valued_weight_pct > resolved_weight_pct order by 1, 2"
        ),
        "select count(*) from mart.fund_virtual_company",
    ),
)


def head_is_scheduled_sql(contract: EnvironmentContract) -> str:
    unscheduled: list[str] = []
    for env, prefixes in contract.unscheduled_id_prefixes().items():
        if not prefixes:
            continue
        names = ", ".join(f"'{n}'" for n in contract.environment_names(env))
        likes = ", ".join(f"'{p}%'" for p in prefixes)
        unscheduled.append(
            f"(lower(trim(coalesce(h.environment, ''))) in ({names})"
            f" and coalesce(h.universe_id, '') like any (array[{likes}]))"
        )
    return f"not ({' or '.join(unscheduled)})" if unscheduled else "true"


def _rank_values(ranks: tuple[tuple[str, int], ...]) -> str:
    return ", ".join(f"('{f}', {r})" for f, r in ranks) or "(null::text, null::integer)"


def render_invariants(contract: EnvironmentContract) -> tuple[Invariant, ...]:
    replacements = {
        "__EXPECTED_VALUES__": ", ".join(f"('{key}', '{issuer_class}')" for key, issuer_class in EXPECTED_INPUTS),
        "{PRIMARY_MARKET_PRICE_PARSER}": PRIMARY_MARKET_PRICE_PARSER,
        "__MARKET_PRICE_PARSER_RANKS__": _rank_values(MARKET_PRICE_PARSER_RANKS),
        "__HEAD_IS_SCHEDULED__": head_is_scheduled_sql(contract),
    }

    def _fill(sql: str) -> str:
        for token, value in replacements.items():
            sql = sql.replace(token, value)
        return sql

    return tuple(
        Invariant(
            inv.id,
            inv.claim,
            _fill(inv.violations),
            _fill(inv.population),
            None if inv.signals is None else _fill(inv.signals),
        )
        for inv in _TEMPLATES
    )


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
        e["invariant"]: Exemption(e["invariant"], e["issue"], date.fromisoformat(e["expires"]), e["reason"])
        for e in raw["exemptions"]
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

    def _row_str(row: tuple[Any, ...]) -> str:
        return " ".join(map(str, row))

    with connect(database_url) as connection:
        for inv in invariants:
            population = _rows(connection, inv.population)[0][0]
            violations = _rows(connection, inv.violations)
            exemption = exemptions.get(inv.id)
            if inv.signals is not None:
                for row in _rows(connection, inv.signals):
                    print(f"  SIGNAL   {inv.id}: {_row_str(row)}")
            if violations:
                if exemption and exemption.expires >= today:
                    print(
                        f"  DEFERRED {inv.id}: {len(violations)} violation(s), until {exemption.expires} ({exemption.issue}): {exemption.reason}"
                    )
                else:
                    detail = "; ".join(_row_str(row) for row in violations[:5])
                    expired = (
                        f" The exemption under {exemption.issue} expired {exemption.expires}." if exemption else ""
                    )
                    failures.append(
                        f"{inv.id}: {len(violations)} violation(s) — {inv.claim}.{expired} Offending rows: {detail}"
                    )
                continue
            if population == 0:
                print(f"  EMPTY    {inv.id}: examined 0 rows, so it proved nothing")
                if require_coverage:
                    failures.append(f"{inv.id}: examined 0 rows where real data was required")
                continue
            if exemption:
                failures.append(
                    f"{inv.id}: exempt under {exemption.issue} but no longer violated across {population} row(s). "
                    "Remove the exemption"
                )
                continue
            print(f"  ok       {inv.id}: {population} row(s) examined")

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
