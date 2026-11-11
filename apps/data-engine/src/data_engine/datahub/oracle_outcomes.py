"""Record external oracle comparisons for a capture run (#1523 S2)."""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

import psycopg
from factors.production_topt import MetricAvailability, ToptCoreSnapshotInput
from truealpha_contracts.environment_contract import SentinelKnownAnswer, load_sentinel_known_answers
from truealpha_contracts.metrics import INPUT_KEY_ALIASES, METRICS
from truealpha_contracts.models import DataSource
from truealpha_contracts.oracle_policy import load_oracle_policy
from truealpha_runtime.storage import StorageError

from data_engine import raw_store
from data_engine.datahub.production_topt.source_registrations import AsOfAxis, knowable_instant
from data_engine.datahub.production_topt.universe_plane import UNIVERSE_SOURCES
from data_engine.quality import vendor_oracle

OPERATOR_COMPARATOR = "operator_listed_units"
OPERATOR_FIELD = "listed_units"
OPERATOR_KIND = "operator"

INDEPENDENT_COMPARATOR = "independent_parse_annual_figures"
INDEPENDENT_KIND = "independent_parse"
INDEPENDENT_PARSERS: dict[str, Callable[[dict[str, Any], date], vendor_oracle.VendorFact | None]] = {
    "revenue": vendor_oracle.revenue,
    "gross_profit": vendor_oracle.gross_profit,
    "total_assets": vendor_oracle.total_assets,
}
INDEPENDENT_UNIT = "USD"

KNOWN_ANSWERS_COMPARATOR = "sentinel_known_answers"
FILING_KIND = "filing"
FILING_SOURCE = "sec-edgar-filing"

AGREE = "agree"
DISAGREE = "disagree"
NOT_COMPARABLE = "not_comparable"

NO_SERVED_VALUE = "no_served_value"
NO_OPERATOR_ROW = "no_operator_row"
NO_MARKET_CAP = "no_market_cap"
NO_PRICE = "no_price"
NO_DOCUMENT = "no_document"
NO_ORACLE_VALUE = "no_oracle_value"
ZERO_ORACLE_VALUE = "zero_oracle_value"
UNIT_NOT_USD = "unit_not_usd"
SUPERSEDED = "superseded"
PRIOR_PERIOD = "prior_period"
NO_OBSERVATION = "no_observation"
SIGN_DIFFERS = "sign_differs"

_FROZEN_FACTOR_INPUTS_SQL = """
    select member.factor_input
    from staging.topt_core_snapshots snapshot
    join staging.topt_core_snapshot_members member on member.snapshot_id = snapshot.snapshot_id
    where snapshot.run_id = %s
    order by member.snapshot_id, member.factor_input ->> 'issuer_id'
"""

_OBSERVATION_FETCH_SQL = """
    select observation.observation_id, vintage.raw_fetch_id, payload.normalized_payload ->> 'currency'
    from staging.capture_normalized_observations observation
    join raw.capture_source_vintages vintage on vintage.source_vintage_id = observation.source_vintage_id
    left join staging.capture_observation_payloads payload on payload.observation_id = observation.observation_id
    where observation.observation_id = any(%s) and vintage.raw_fetch_id is not null
"""

_INSERT_SQL = """
    insert into mart.oracle_outcomes
        (run_id, subject_id, field, period, served_value, oracle_value, oracle_kind, source, observation_id,
         knowable_at, ratio, tolerance, result, reason, compared_at)
    values (%(run_id)s, %(subject_id)s, %(field)s, %(period)s, %(served_value)s, %(oracle_value)s, %(oracle_kind)s,
            %(source)s, %(observation_id)s, %(knowable_at)s, %(ratio)s, %(tolerance)s, %(result)s, %(reason)s,
            %(compared_at)s)
    on conflict on constraint oracle_outcomes_once do nothing
"""


class Logger(Protocol):
    def warning(self, message: str) -> None: ...


@dataclass(frozen=True)
class OperatorFigure:
    row_id: int
    ticker: str
    as_of: date
    source: str
    market_cap: Decimal | None
    last_sale_price: Decimal | None
    knowable_at: datetime


@dataclass(frozen=True)
class Outcome:
    subject_id: str
    field: str
    oracle_kind: str
    period: str
    served_value: Decimal | None
    oracle_value: Decimal | None
    source: str
    observation_id: str | None
    knowable_at: datetime | None
    ratio: Decimal | None
    tolerance: Decimal | None
    result: str
    reason: str | None


def _run_payload_pairs(connection: psycopg.Connection[Any], run_id: str, key_col: str, val_col: str) -> dict[str, str]:
    key_expr = (
        f"upper(p.normalized_payload ->> '{key_col}')"
        if key_col == "ticker"
        else f"p.normalized_payload ->> '{key_col}'"
    )
    val_expr = (
        f"upper(p.normalized_payload ->> '{val_col}')"
        if val_col == "ticker"
        else f"p.normalized_payload ->> '{val_col}'"
    )
    rows = connection.execute(
        f"""
        select distinct {key_expr}, {val_expr}
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_observation_payloads p on p.observation_id = oo.observation_id
        where ob.run_id = %s
          and p.normalized_payload ->> '{key_col}' is not null
          and p.normalized_payload ->> '{val_col}' is not null
        """,  # noqa: S608
        (run_id,),
    ).fetchall()
    return {str(k): str(v) for k, v in rows if k and v}


def run_tickers(connection: psycopg.Connection[Any], run_id: str) -> dict[str, str]:
    return _run_payload_pairs(connection, run_id, "listing_id", "ticker")


def served_listed_units(factor_input: ToptCoreSnapshotInput) -> Decimal | None:
    counts: list[Decimal] = []
    for component in factor_input.market_value_components:
        metric = component.shares_outstanding
        if metric.availability is not MetricAvailability.AVAILABLE or metric.value is None:
            return None
        counts.append(metric.value)
    if not counts:
        return None
    return counts[0] if len(counts) > 1 and len(set(counts)) == 1 else sum(counts, start=Decimal(0))


def _operator_outcome(
    factor_input: ToptCoreSnapshotInput,
    period: str,
    served: Decimal | None,
    result: str,
    *,
    reason: str | None = None,
    figure: OperatorFigure | None = None,
    witness: Decimal | None = None,
    ratio: Decimal | None = None,
    tolerance: Decimal | None = None,
) -> Outcome:
    return Outcome(
        subject_id=factor_input.issuer_id,
        field=OPERATOR_FIELD,
        oracle_kind=OPERATOR_KIND,
        period=period,
        served_value=served,
        oracle_value=witness,
        source=figure.source if figure is not None else DataSource.NASDAQ_INDEX.value,
        observation_id=None if figure is None else f"etf-constituent-fact:{figure.row_id}",
        knowable_at=None if figure is None else figure.knowable_at,
        ratio=ratio,
        tolerance=tolerance,
        result=result,
        reason=reason,
    )


def compare_listed_units(
    factor_input: ToptCoreSnapshotInput,
    figures: dict[str, OperatorFigure],
    *,
    ticker: str | None,
    list_as_of: date,
    tolerance: Decimal,
) -> Outcome:
    served = served_listed_units(factor_input)
    figure = None if ticker is None else figures.get(ticker)
    if figure is None:
        return _operator_outcome(factor_input, list_as_of.isoformat(), served, NOT_COMPARABLE, reason=NO_OPERATOR_ROW)
    period = figure.as_of.isoformat()
    if figure.market_cap is None:
        return _operator_outcome(factor_input, period, served, NOT_COMPARABLE, reason=NO_MARKET_CAP, figure=figure)
    if figure.last_sale_price is None or figure.last_sale_price <= 0:
        return _operator_outcome(factor_input, period, served, NOT_COMPARABLE, reason=NO_PRICE, figure=figure)
    witness = figure.market_cap / figure.last_sale_price
    if served is None:
        return _operator_outcome(
            factor_input, period, None, NOT_COMPARABLE, reason=NO_SERVED_VALUE, figure=figure, witness=witness
        )
    ratio = served / witness
    return _operator_outcome(
        factor_input,
        period,
        served,
        AGREE if abs(ratio - 1) <= tolerance else DISAGREE,
        figure=figure,
        witness=witness,
        ratio=ratio,
        tolerance=tolerance,
    )


def operator_figures(
    connection: psycopg.Connection[Any], cutoff: datetime
) -> tuple[dict[str, OperatorFigure], date | None]:
    etfs = [source.etf for source in UNIVERSE_SOURCES.values() if source.index_api]
    figures: dict[str, OperatorFigure] = {}
    newest: date | None = None
    for etf in etfs:
        rows = connection.execute(
            """
            select id, ticker, as_of, source, market_cap, last_sale_price, knowable_at
            from staging.etf_constituent_facts
            where etf_symbol = %(etf)s and knowable_at <= %(cutoff)s
              and as_of = (
                  select max(as_of) from staging.etf_constituent_facts
                  where etf_symbol = %(etf)s and knowable_at <= %(cutoff)s
                    and market_cap is not null and last_sale_price is not null and last_sale_price > 0
              )
            order by id
            """,
            {"etf": etf, "cutoff": cutoff},
        ).fetchall()
        for row_id, ticker, as_of, source, market_cap, last_sale_price, knowable_at in rows:
            figures.setdefault(
                str(ticker).upper(),
                OperatorFigure(int(row_id), str(ticker), as_of, str(source), market_cap, last_sale_price, knowable_at),
            )
            newest = as_of if newest is None else max(newest, as_of)
    return figures, newest


def operator_outcomes(
    connection: psycopg.Connection[Any],
    run_id: str,
    cutoff: datetime,
    factor_inputs: Sequence[ToptCoreSnapshotInput],
) -> list[Outcome]:
    figures, list_as_of = operator_figures(connection, cutoff)
    if list_as_of is None:
        return []
    tolerance = load_oracle_policy().relative_tolerance(OPERATOR_COMPARATOR)
    tickers = run_tickers(connection, run_id)
    return [
        compare_listed_units(fi, figures, ticker=tickers.get(fi.listing_id), list_as_of=list_as_of, tolerance=tolerance)
        for fi in factor_inputs
    ]


def _ratio_and_sign(served: Decimal, target: Decimal) -> tuple[Decimal, str | None]:
    if target == 0:
        return (Decimal(1) if served == 0 else Decimal(0)), None
    if served * target < 0:
        return abs(served) / abs(target), SIGN_DIFFERS
    return served / target, None


def compare_independent_parse(
    factor_input: ToptCoreSnapshotInput,
    field: str,
    facts: dict[str, Any] | None,
    *,
    cutoff: date,
    tolerance: Decimal,
    currency: str | None = INDEPENDENT_UNIT,
) -> Outcome:
    metric = getattr(factor_input, field)
    served: Decimal | None = None if metric is None else metric.value
    observation_id: str | None = None if metric is None else metric.input_id

    def outcome(
        period: str,
        result: str,
        *,
        reason: str | None = None,
        witness: vendor_oracle.VendorFact | None = None,
        ratio: Decimal | None = None,
    ) -> Outcome:
        return Outcome(
            subject_id=factor_input.issuer_id,
            field=field,
            oracle_kind=INDEPENDENT_KIND,
            period=period,
            served_value=served,
            oracle_value=None if witness is None else witness.value,
            source=DataSource.SEC.value,
            observation_id=observation_id,
            knowable_at=None if witness is None else knowable_instant(AsOfAxis.ACCESSION, witness.filed),
            ratio=ratio,
            tolerance=None if ratio is None else tolerance,
            result=result,
            reason=reason,
        )

    if facts is None:
        return outcome(cutoff.isoformat(), NOT_COMPARABLE, reason=NO_DOCUMENT)
    witness = INDEPENDENT_PARSERS[field](facts, cutoff)
    if witness is None:
        return outcome(cutoff.isoformat(), NOT_COMPARABLE, reason=NO_ORACLE_VALUE)
    period = witness.period_end.isoformat()
    if served is None:
        return outcome(period, NOT_COMPARABLE, reason=NO_SERVED_VALUE, witness=witness)
    if (currency or getattr(metric, "unit", INDEPENDENT_UNIT)) != INDEPENDENT_UNIT:
        return outcome(period, NOT_COMPARABLE, reason=UNIT_NOT_USD, witness=witness)
    if witness.value == 0:
        return outcome(period, NOT_COMPARABLE, reason=ZERO_ORACLE_VALUE, witness=witness)
    ratio, sign_reason = _ratio_and_sign(served, witness.value)
    if sign_reason:
        return outcome(period, DISAGREE, reason=sign_reason, witness=witness, ratio=ratio)
    return outcome(period, AGREE if abs(ratio - 1) <= tolerance else DISAGREE, witness=witness, ratio=ratio)


def stored_document(connection: psycopg.Connection[Any], fetch_id: int) -> dict[str, Any] | None:
    try:
        document = json.loads(raw_store.get_payload(connection, fetch_id))
    except (LookupError, StorageError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _financial_observation(factor_input: ToptCoreSnapshotInput) -> str | None:
    for field in INDEPENDENT_PARSERS:
        metric = getattr(factor_input, field)
        if metric is not None:
            return str(metric.input_id)
    return None


def independent_parse_outcomes(
    connection: psycopg.Connection[Any], cutoff: datetime, factor_inputs: Sequence[ToptCoreSnapshotInput]
) -> list[Outcome]:
    observations = {fi.issuer_id: _financial_observation(fi) for fi in factor_inputs}
    wanted = sorted({obs for obs in observations.values() if obs is not None})
    fetch_rows = connection.execute(_OBSERVATION_FETCH_SQL, (wanted,)).fetchall()
    fetch_of = {row[0]: row[1] for row in fetch_rows}
    currency_of = {row[0]: row[2] for row in fetch_rows}
    tolerance = load_oracle_policy().relative_tolerance(INDEPENDENT_COMPARATOR)
    day = cutoff.astimezone(UTC).date()
    outcomes: list[Outcome] = []
    for fi in factor_inputs:
        obs = observations[fi.issuer_id]
        fetch_id = None if obs is None else fetch_of.get(obs)
        currency = None if obs is None else currency_of.get(obs)
        facts = None if fetch_id is None else stored_document(connection, fetch_id)
        outcomes.extend(
            compare_independent_parse(fi, field, facts, cutoff=day, tolerance=tolerance, currency=currency)
            for field in INDEPENDENT_PARSERS
        )
    return outcomes


def canonical_metric(metric: str) -> str:
    return INPUT_KEY_ALIASES.get(metric, metric)


def _compare_band_answer(
    answer: SentinelKnownAnswer, served_value: Decimal
) -> tuple[Decimal, Decimal, Decimal, str, str | None]:
    lower, upper = Decimal(str(answer.lower_bound)), Decimal(str(answer.upper_bound))
    midpoint = (lower + upper) / Decimal(2)
    tolerance = Decimal(str(answer.relative_tolerance)) if answer.relative_tolerance else (upper - midpoint) / midpoint
    ratio, sign_reason = _ratio_and_sign(served_value, midpoint)
    if sign_reason:
        return midpoint, ratio, tolerance, DISAGREE, sign_reason
    return midpoint, ratio, tolerance, (AGREE if lower <= served_value <= upper else DISAGREE), None


def _compare_scalar_answer(
    answer: SentinelKnownAnswer, served_value: Decimal, policy_tolerance: Decimal
) -> tuple[Decimal, Decimal, Decimal, str, str | None]:
    oracle_value = Decimal(str(answer.expected_value))
    tolerance = Decimal(str(answer.relative_tolerance)) if answer.relative_tolerance else policy_tolerance
    ratio, sign_reason = _ratio_and_sign(served_value, oracle_value)
    if sign_reason:
        return oracle_value, ratio, tolerance, DISAGREE, sign_reason
    if oracle_value == 0:
        return oracle_value, ratio, tolerance, (AGREE if served_value == 0 else DISAGREE), None
    return oracle_value, ratio, tolerance, (AGREE if abs(ratio - 1) <= tolerance else DISAGREE), None


def compare_known_answer(
    answer: SentinelKnownAnswer,
    served_value: Decimal | None,
    *,
    subject_id: str | None = None,
    actual_period_end: str | None,
    observation_id: str | None,
    knowable_at: datetime | None,
    policy_tolerance: Decimal,
) -> Outcome:
    canonical_field = canonical_metric(answer.metric)
    source = answer.accession or FILING_SOURCE
    resolved_subject = subject_id or answer.issuer_id

    def outcome(
        result: str,
        *,
        reason: str | None = None,
        oracle_value: Decimal | None = None,
        ratio: Decimal | None = None,
        tolerance: Decimal | None = None,
        obs_id: str | None = observation_id,
        know_at: datetime | None = knowable_at,
    ) -> Outcome:
        return Outcome(
            subject_id=resolved_subject,
            field=canonical_field,
            oracle_kind=FILING_KIND,
            period=answer.period_end,
            served_value=served_value,
            oracle_value=oracle_value,
            source=source,
            observation_id=obs_id,
            knowable_at=know_at,
            ratio=ratio,
            tolerance=tolerance,
            result=result,
            reason=reason,
        )

    if actual_period_end is not None and str(actual_period_end) > answer.period_end:
        return outcome(NOT_COMPARABLE, reason=SUPERSEDED)
    if actual_period_end is not None and str(actual_period_end) < answer.period_end:
        return outcome(NOT_COMPARABLE, reason=PRIOR_PERIOD)
    if served_value is None:
        return outcome(NOT_COMPARABLE, reason=NO_SERVED_VALUE)
    if observation_id is None or knowable_at is None:
        return outcome(NOT_COMPARABLE, reason=NO_OBSERVATION, obs_id=None, know_at=None)

    oracle_val, ratio, tolerance, res, reason = (
        _compare_band_answer(answer, served_value)
        if answer.kind == "band"
        else _compare_scalar_answer(answer, served_value, policy_tolerance)
    )
    return outcome(res, reason=reason, oracle_value=oracle_val, ratio=ratio, tolerance=tolerance)


def _query_gppe_by_issuer(
    connection: psycopg.Connection[Any], run_id: str, cutoff: datetime
) -> dict[str, tuple[Decimal | None, str | None, str | None, datetime | None]]:
    rows = connection.execute(
        "select issuer_id, gppe, operating_period_end, input_observation_ids, cutoff, result_id from mart.topt_core_results where run_id = %s",
        (run_id,),
    ).fetchall()
    gppe_by_issuer: dict[str, tuple[Decimal | None, str | None, str | None, datetime | None]] = {}
    for iss_id, gppe, op_period, obs_ids, cut, res_id in rows:
        period_str = (
            op_period.isoformat()
            if isinstance(op_period, (date, datetime))
            else (str(op_period) if op_period else None)
        )
        entry = (gppe, period_str, obs_ids[0] if obs_ids else res_id, cut or cutoff)
        gppe_by_issuer[iss_id] = entry
        if str(iss_id).startswith("issuer:cik:"):
            gppe_by_issuer[str(iss_id).removeprefix("issuer:cik:")] = entry
    return gppe_by_issuer


def _query_fin_by_issuer(
    connection: psycopg.Connection[Any], run_id: str
) -> dict[str, tuple[str, datetime, dict[str, Any]]]:
    rows = connection.execute(
        """
        select coalesce(p.normalized_payload ->> 'issuer_id', ''), o.observation_id, o.knowable_at, p.normalized_payload
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        where ob.run_id = %s and o.semantic_type = 'financial-fact'
        order by o.observation_id
        """,
        (run_id,),
    ).fetchall()
    fin_by_issuer: dict[str, tuple[str, datetime, dict[str, Any]]] = {}
    for iss_id, obs_id, know_at, payload in rows:
        keys = [iss_id, *([str(iss_id).removeprefix("issuer:cik:")] if str(iss_id).startswith("issuer:cik:") else [])]
        for key in keys:
            held = fin_by_issuer.get(key)
            if held is None or (know_at, obs_id) >= (held[1], held[0]):
                fin_by_issuer[key] = (obs_id, know_at, payload)
    return fin_by_issuer


_PAYLOAD_PERIOD_KEYS = {
    "revenue": "revenue_period_end",
    "gross_profit": "operating_period_end",
    "shares_outstanding": "shares_period_end",
}


def _extract_metric_from_payload(
    payload: dict[str, Any], canonical_field: str
) -> tuple[Decimal | None, str | None, datetime | None]:
    if canonical_field in _PAYLOAD_PERIOD_KEYS:
        raw = payload.get(canonical_field)
        return (None if raw is None else Decimal(str(raw)), payload.get(_PAYLOAD_PERIOD_KEYS[canonical_field]), None)
    if canonical_field == "total_assets":
        raw = payload.get("total_assets")
        return (
            None if raw is None else Decimal(str(raw)),
            payload.get("operating_period_end") or payload.get("tangible_equity_period_end"),
            None,
        )
    if canonical_field == "employees_total":
        raw = payload.get("employees_total") or payload.get("headcount")
        hc_vintage = (payload.get("vintage") or {}).get("headcount") or {}
        period = (
            hc_vintage.get("period_end") or payload.get("revenue_period_end") or payload.get("operating_period_end")
        )
        know_at = None
        if hc_vintage.get("knowable_at"):
            try:
                know_at = datetime.fromisoformat(hc_vintage["knowable_at"])
            except Exception:
                pass
        return (None if raw is None else Decimal(str(raw)), period, know_at)
    return (None, None, None)


def _extract_metric_from_factor_input(
    fi: ToptCoreSnapshotInput, canonical_field: str
) -> tuple[Decimal | None, str | None, datetime | None]:
    metric = {
        "revenue": fi.revenue,
        "gross_profit": fi.gross_profit,
        "employees_total": fi.headcount,
        "total_assets": fi.total_assets,
    }.get(canonical_field)
    return (metric.value, str(metric.input_id), metric.knowable_at) if metric is not None else (None, None, None)


def run_issuer_tickers(connection: psycopg.Connection[Any], run_id: str) -> dict[str, str]:
    issuers = _run_payload_pairs(connection, run_id, "ticker", "issuer_id")
    listing_tickers = run_tickers(connection, run_id)
    if listing_tickers:
        for (fi,) in connection.execute(_FROZEN_FACTOR_INPUTS_SQL, (run_id,)).fetchall():
            if isinstance(fi, dict):
                lid, iid = fi.get("listing_id"), fi.get("issuer_id")
                if lid and iid and lid in listing_tickers:
                    issuers.setdefault(listing_tickers[lid], str(iid))
    return issuers


def _resolve_answer_context(
    answer: SentinelKnownAnswer,
    canonical_field: str,
    *,
    issuer_id: str,
    gppe_by_issuer: dict[str, tuple[Decimal | None, str | None, str | None, datetime | None]],
    fin_by_issuer: dict[str, tuple[str, datetime, dict[str, Any]]],
    by_issuer_input: dict[str, ToptCoreSnapshotInput],
    cutoff: datetime,
) -> tuple[Decimal | None, str | None, str | None, datetime | None]:
    fi = (
        by_issuer_input.get(issuer_id)
        or by_issuer_input.get(answer.issuer_id)
        or by_issuer_input.get(f"issuer:cik:{answer.cik}")
    )
    if canonical_field == "gross_profit_per_employee":
        gppe_entry = (
            gppe_by_issuer.get(issuer_id) or gppe_by_issuer.get(answer.issuer_id) or gppe_by_issuer.get(answer.cik)
        )
        if gppe_entry is not None:
            return gppe_entry
        return (
            (None, None, fi.observation_ids[0] if fi and fi.observation_ids else None, cutoff)
            if fi
            else (None, None, None, None)
        )

    fin_entry = fin_by_issuer.get(issuer_id) or fin_by_issuer.get(answer.issuer_id) or fin_by_issuer.get(answer.cik)
    observation_id: str | None = None
    knowable_at: datetime | None = None
    actual_period_end: str | None = None
    served_value: Decimal | None = None
    if fin_entry is not None:
        observation_id, knowable_at, payload = fin_entry
        served_value, actual_period_end, custom_know = _extract_metric_from_payload(payload, canonical_field)
        if custom_know is not None:
            knowable_at = custom_know
    if served_value is None and fi is not None:
        val, obs_id, know = _extract_metric_from_factor_input(fi, canonical_field)
        if val is not None:
            served_value, observation_id, knowable_at = val, observation_id or obs_id, knowable_at or know
    return (served_value, actual_period_end, observation_id, knowable_at)


def known_answer_outcomes(
    connection: psycopg.Connection[Any],
    run_id: str,
    cutoff: datetime,
    factor_inputs: Sequence[ToptCoreSnapshotInput],
) -> list[Outcome]:
    catalog = load_sentinel_known_answers()
    try:
        policy_tolerance = load_oracle_policy().relative_tolerance(KNOWN_ANSWERS_COMPARATOR)
    except (KeyError, ValueError):
        policy_tolerance = Decimal("0.01")

    issuers_by_ticker = run_issuer_tickers(connection, run_id)
    by_issuer_input = {fi.issuer_id: fi for fi in factor_inputs}
    run_issuers = set(by_issuer_input) | set(issuers_by_ticker.values())
    gppe_by_issuer = _query_gppe_by_issuer(connection, run_id, cutoff)
    fin_by_issuer = _query_fin_by_issuer(connection, run_id)

    all_run_issuers = run_issuers | set(fin_by_issuer) | set(gppe_by_issuer)
    all_run_ciks = {
        iss.removeprefix("issuer:cik:") if iss.startswith("issuer:cik:") else iss
        for iss in all_run_issuers
        if iss.startswith("issuer:cik:") or not iss.startswith("issuer:")
    }

    outcomes: list[Outcome] = []
    for answer in catalog.answers:
        canonical_field = canonical_metric(answer.metric)
        if canonical_field not in METRICS:
            continue
        issuer_id = (
            issuers_by_ticker.get(answer.ticker.upper())
            or (answer.issuer_id if answer.issuer_id in all_run_issuers else None)
            or (f"issuer:cik:{answer.cik}" if f"issuer:cik:{answer.cik}" in all_run_issuers else None)
        )
        if issuer_id is None and answer.cik not in all_run_ciks:
            continue
        target_subject_id = issuer_id or answer.issuer_id
        served_val, period, obs_id, know_at = _resolve_answer_context(
            answer,
            canonical_field,
            issuer_id=target_subject_id,
            gppe_by_issuer=gppe_by_issuer,
            fin_by_issuer=fin_by_issuer,
            by_issuer_input=by_issuer_input,
            cutoff=cutoff,
        )
        outcomes.append(
            compare_known_answer(
                answer,
                served_val,
                subject_id=target_subject_id,
                actual_period_end=period,
                observation_id=obs_id,
                knowable_at=know_at,
                policy_tolerance=policy_tolerance,
            )
        )
    return outcomes


def record_oracle_outcomes(connection: psycopg.Connection[Any], run_id: str, *, compared_at: datetime) -> int:
    status = connection.execute("select cutoff from mart.topt_capture_status where run_id = %s", (run_id,)).fetchone()
    if status is None:
        return 0
    cutoff = status[0]
    factor_inputs = [
        ToptCoreSnapshotInput.model_validate(row[0]) for row in connection.execute(_FROZEN_FACTOR_INPUTS_SQL, (run_id,))
    ]
    outcomes = [
        *operator_outcomes(connection, run_id, cutoff, factor_inputs),
        *independent_parse_outcomes(connection, cutoff, factor_inputs),
        *known_answer_outcomes(connection, run_id, cutoff, factor_inputs),
    ]
    connection.execute("set local lock_timeout = '5s'")
    written = 0
    for outcome in outcomes:
        cursor = connection.execute(
            _INSERT_SQL, {"run_id": run_id, **dataclasses.asdict(outcome), "compared_at": compared_at}
        )
        written += cursor.rowcount
    return written


def record_oracle_outcomes_safely(
    connection: psycopg.Connection[Any], run_id: str, *, compared_at: datetime, log: Logger
) -> int | None:
    try:
        with connection.transaction():
            return record_oracle_outcomes(connection, run_id, compared_at=compared_at)
    except Exception as error:
        log.warning(f"oracle outcomes of {run_id} were not written: {type(error).__name__}: {error}")
        return None


_WILSON_Z_95 = 1.959963984540054


def wilson_95_upper_bound(disagrees: int, compared: int) -> float | None:
    if compared <= 0:
        return None
    z2 = _WILSON_Z_95 * _WILSON_Z_95
    p = disagrees / compared
    centre = p + z2 / (2.0 * compared)
    half_width = _WILSON_Z_95 * math.sqrt((p * (1.0 - p) + z2 / (4.0 * compared)) / compared)
    return min(1.0, round((centre + half_width) / (1.0 + z2 / compared), 6))


def _iso_utc(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value).strip() or None


def _row_attr(row: Mapping[str, Any] | Outcome, key: str, default: Any = None) -> Any:
    return row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)


def _summarize_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    agree = sum(1 for item in rows if item["result"] == "agree")
    disagree = sum(1 for item in rows if item["result"] == "disagree")
    not_comparable = sum(1 for item in rows if item["result"] == "not_comparable")
    compared = agree + disagree
    timestamps = [item["compared_at_iso"] for item in rows if item["compared_at_iso"] is not None]
    return {
        "compared": compared,
        "agree": agree,
        "disagree": disagree,
        "not_comparable": not_comparable,
        "newest_compared_at": max(timestamps) if timestamps else None,
        "wilson_95_upper_bound": wilson_95_upper_bound(disagree, compared),
    }


def rollup_oracle_outcomes(rows: Sequence[Mapping[str, Any] | Outcome]) -> dict[str, Any]:
    deduped: dict[tuple[str | None, str, str], dict[str, Any]] = {}
    deduped_by_comparator: dict[tuple[str, str | None, str, str], dict[str, Any]] = {}

    for row in rows:
        obs_raw = _row_attr(row, "observation_id")
        observation_id = str(obs_raw) if obs_raw is not None else None
        subject_id = str(_row_attr(row, "subject_id", ""))
        field = str(_row_attr(row, "field", ""))
        reason_raw = _row_attr(row, "reason")
        oracle_kind_raw = _row_attr(row, "oracle_kind")
        oracle_kind = str(oracle_kind_raw) if oracle_kind_raw is not None else None
        compared_at_iso = _iso_utc(_row_attr(row, "compared_at"))
        normalized = {
            "observation_id": observation_id,
            "subject_id": subject_id,
            "field": field,
            "result": str(_row_attr(row, "result", "")),
            "reason": str(reason_raw) if reason_raw is not None else None,
            "oracle_kind": oracle_kind,
            "compared_at_iso": compared_at_iso,
        }
        if oracle_kind:
            comp_key = (oracle_kind, observation_id, subject_id, field)
            comp_existing = deduped_by_comparator.get(comp_key)
            if comp_existing is None or (compared_at_iso or "") >= (comp_existing["compared_at_iso"] or ""):
                deduped_by_comparator[comp_key] = normalized
        key = (observation_id, subject_id, field)
        existing = deduped.get(key)
        if existing is None or (compared_at_iso or "") >= (existing["compared_at_iso"] or ""):
            deduped[key] = normalized

    deduped_rows = list(deduped.values())
    by_field_rows: dict[str, list[dict[str, Any]]] = {}
    for item in deduped_rows:
        by_field_rows.setdefault(item["field"], []).append(item)

    by_comparator_rows: dict[str, list[dict[str, Any]]] = {}
    for item in deduped_by_comparator.values():
        by_comparator_rows.setdefault(str(item["oracle_kind"]), []).append(item)

    comparators = {
        kind: _summarize_counts(kind_rows)
        for kind, kind_rows in sorted(by_comparator_rows.items())
        if not (kind == "independent_parse" and all(r["reason"] == "no_document" for r in kind_rows))
    }
    return {
        **_summarize_counts(deduped_rows),
        "by_field": {f: _summarize_counts(f_rows) for f, f_rows in sorted(by_field_rows.items())},
        "comparators": comparators,
    }


def _query_oracle_rows(
    connection: psycopg.Connection[Any], cols: tuple[str, ...], where_sql: str = "", params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    if not hasattr(connection, "execute"):
        return []
    try:
        rows = connection.execute(
            f"select {', '.join(cols)} from mart.oracle_outcomes {where_sql} order by compared_at asc, recorded_at asc, outcome_id asc",  # noqa: S608
            params,
        ).fetchall()
    except Exception:
        return []
    return [dict(zip(cols, row, strict=True)) for row in rows]


def load_oracle_outcomes_rollup(connection: psycopg.Connection[Any], *, run_id: str | None = None) -> dict[str, Any]:
    cols = ("observation_id", "subject_id", "field", "result", "reason", "oracle_kind", "compared_at")
    where = "where run_id = %s" if run_id is not None else ""
    params = (run_id,) if run_id is not None else ()
    return rollup_oracle_outcomes(_query_oracle_rows(connection, cols, where, params))


def _outcome_to_dict(row: Mapping[str, Any] | Outcome) -> dict[str, Any]:
    return {**dataclasses.asdict(row), "compared_at": None} if isinstance(row, Outcome) else dict(row)


_SOURCE_SUBSTRING_ORIGINS = (
    ("moomoo", "origin:moomoo-financials:v1"),
    ("twelve", "origin:twelve-data:v1"),
    ("yahoo", "origin:yahoo:v1"),
    ("nport", "origin:sec-nport:v1"),
    ("nasdaq", "origin:nasdaq-index:v1"),
)


def _resolve_row_origin_id(row: Mapping[str, Any]) -> str | None:
    oracle_kind = str(row.get("oracle_kind") or "")
    field = canonical_metric(str(row.get("field") or ""))
    if oracle_kind == OPERATOR_KIND or field == "gross_profit_per_employee":
        return None
    for key in ("origin_id", "lineage", "origin"):
        explicit = row.get(key)
        if isinstance(explicit, str) and explicit.startswith("origin:"):
            return explicit
    source = str(row.get("source") or "")
    if source.startswith("origin:"):
        return source
    lowered = source.lower()
    for token, origin_id in _SOURCE_SUBSTRING_ORIGINS:
        if token in lowered:
            return origin_id
    return "origin:sec-company-facts:v1" if oracle_kind in (FILING_KIND, INDEPENDENT_KIND) else None


def _is_at_or_before_cutoff(row: Mapping[str, Any], cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    cutoff_iso = _iso_utc(cutoff) or ""
    stamp = _iso_utc(row.get("compared_at")) or _iso_utc(row.get("knowable_at"))
    return True if stamp is None else stamp <= cutoff_iso


def _row_compared_iso(item: Mapping[str, Any]) -> str:
    return _iso_utc(item.get("compared_at")) or _iso_utc(item.get("knowable_at")) or ""


def _dedupe_origin_mass_rows(
    rows: Sequence[Mapping[str, Any] | Outcome], cutoff: datetime | None
) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for raw in rows:
        item = _outcome_to_dict(raw)
        if not _is_at_or_before_cutoff(item, cutoff):
            continue
        oracle_kind = str(item.get("oracle_kind") or "")
        observation_id = str(item.get("observation_id") or "")
        subject_id = str(item.get("subject_id") or "")
        field = canonical_metric(str(item.get("field") or ""))
        if not (oracle_kind and subject_id and field):
            continue
        compared_iso = _row_compared_iso(item)
        item["_compared_iso"] = compared_iso
        key = (oracle_kind, observation_id, subject_id, field)
        existing = deduped.get(key)
        if existing is None or compared_iso >= existing["_compared_iso"]:
            deduped[key] = item
    return list(deduped.values())


_OUTCOME_MASS_DELTAS: dict[tuple[str, str], tuple[Decimal, Decimal]] = {
    (FILING_KIND, AGREE): (Decimal("1"), Decimal("0")),
    (FILING_KIND, DISAGREE): (Decimal("0"), Decimal("1")),
}


def oracle_outcome_mass_by_origin(
    rows: Sequence[Mapping[str, Any] | Outcome], *, cutoff: datetime | None = None
) -> dict[str, tuple[Decimal, Decimal]]:
    totals: dict[str, tuple[Decimal, Decimal]] = {}
    for item in _dedupe_origin_mass_rows(rows, cutoff):
        origin_id = _resolve_row_origin_id(item)
        if origin_id is None:
            continue
        field = canonical_metric(str(item.get("field") or ""))
        succ_d, fail_d = _OUTCOME_MASS_DELTAS.get(
            (str(item.get("oracle_kind") or ""), str(item.get("result") or "")), (Decimal("0"), Decimal("0"))
        )
        if succ_d == 0 and fail_d == 0:
            totals.setdefault(origin_id, (Decimal("0"), Decimal("0")))
            continue
        keys = (
            (origin_id, f"{origin_id}:{field}", f"{origin_id}:headcount")
            if field == "employees_total"
            else (origin_id, f"{origin_id}:{field}")
        )
        for key in keys:
            succ, fail = totals.get(key, (Decimal("0"), Decimal("0")))
            totals[key] = (succ + succ_d, fail + fail_d)
    return totals


def _relative_delta_from_row(row: Mapping[str, Any], result: str) -> Decimal | None:
    served_raw, oracle_raw = row.get("served_value"), row.get("oracle_value")
    if served_raw is not None and oracle_raw is not None:
        served_val, oracle_val = Decimal(str(served_raw)), Decimal(str(oracle_raw))
        if served_val * oracle_val < 0:
            return Decimal("1")
        denom = max(abs(served_val), abs(oracle_val))
        return Decimal("0") if denom == 0 else abs(served_val - oracle_val) / denom
    ratio_raw = row.get("ratio")
    if ratio_raw is None:
        return None
    ratio_val = Decimal(str(ratio_raw))
    return (
        ratio_val if (result == AGREE and Decimal("0") <= ratio_val < Decimal("0.5")) else abs(ratio_val - Decimal("1"))
    )


def _agreement_from_outcome_row(row: Mapping[str, Any]) -> Decimal:
    result = str(row.get("result") or "")
    if str(row.get("reason") or "") == SIGN_DIFFERS:
        return Decimal("0.25")
    delta = _relative_delta_from_row(row, result)
    if delta is None:
        return Decimal("1") if result == AGREE else Decimal("0.25")
    tol_raw = row.get("tolerance")
    if (
        result == AGREE
        and tol_raw is not None
        and (tol_val := Decimal(str(tol_raw))) > Decimal("0.01")
        and delta <= tol_val
    ):
        return Decimal("1")
    if delta <= Decimal("0.001"):
        return Decimal("1") if result == AGREE else Decimal("0.90")
    if delta <= Decimal("0.01"):
        return Decimal("0.90")
    return Decimal("0.65") if delta <= Decimal("0.05") else Decimal("0.25")


def oracle_cell_agreement(
    rows: Sequence[Mapping[str, Any] | Outcome],
    *,
    subject_ids: Sequence[str] | str,
    field: str,
    cutoff: datetime | None = None,
) -> tuple[Decimal, tuple[str, ...]]:
    targets = {subject_ids} if isinstance(subject_ids, str) else {s for s in subject_ids if s}
    for s in list(targets):
        if s.startswith("issuer:cik:"):
            targets.add(s.removeprefix("issuer:cik:"))
        elif s.isdigit():
            targets.add(f"issuer:cik:{s.zfill(10)}")
    canonical_field = canonical_metric(field)
    candidate_fields = {canonical_field, field, *(("headcount",) if canonical_field == "employees_total" else ())}

    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in rows:
        item = _outcome_to_dict(raw)
        subj = str(item.get("subject_id") or "")
        row_field = str(item.get("field") or "")
        if (
            not _is_at_or_before_cutoff(item, cutoff)
            or subj not in targets
            or (row_field not in candidate_fields and canonical_metric(row_field) not in candidate_fields)
        ):
            continue
        compared_iso = _row_compared_iso(item)
        item["_compared_iso"] = compared_iso
        key = (str(item.get("oracle_kind") or "oracle"), subj, canonical_field)
        existing = deduped.get(key)
        if existing is None or compared_iso >= existing["_compared_iso"]:
            deduped[key] = item

    comparable = [item for item in deduped.values() if str(item.get("result") or "") in (AGREE, DISAGREE)]
    if not comparable:
        return Decimal("1"), ("oracle:none",)
    scores = [_agreement_from_outcome_row(item) for item in comparable]
    reasons = {"oracle:agree" if str(item.get("result") or "") == AGREE else "oracle:disagree" for item in comparable}
    return min(scores), tuple(sorted(reasons))


def load_oracle_confidence_inputs(
    connection: psycopg.Connection[Any], *, run_id: str | None = None, cutoff: datetime | None = None
) -> list[dict[str, Any]]:
    cols = (
        "observation_id",
        "subject_id",
        "field",
        "period",
        "served_value",
        "oracle_value",
        "oracle_kind",
        "source",
        "knowable_at",
        "ratio",
        "tolerance",
        "result",
        "reason",
        "compared_at",
    )
    if cutoff is not None:
        return _query_oracle_rows(connection, cols, "where compared_at <= %s", (cutoff,))
    if run_id is not None:
        return _query_oracle_rows(connection, cols, "where run_id = %s", (run_id,))
    return _query_oracle_rows(connection, cols)
