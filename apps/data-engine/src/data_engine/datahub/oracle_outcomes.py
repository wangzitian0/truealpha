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
from factors.panel import GPPE_BANKING_TCE_SPEC, GPPE_V0_SPEC
from factors.production_topt import MetricAvailability, ToptCoreSnapshotInput
from truealpha_contracts.metrics import INPUT_KEY_ALIASES, METRICS
from truealpha_contracts.models import DataSource
from truealpha_runtime.storage import StorageError

from data_engine import raw_store
from data_engine.contracts.oracle_policy import load_oracle_policy
from data_engine.contracts.sentinel_known_answers import SentinelKnownAnswer, load_sentinel_known_answers
from data_engine.datahub._helpers import CAPTURE_OBSERVATION_JOIN, fetch_topt_cutoff
from data_engine.datahub.capture_core.source_registrations import AsOfAxis, knowable_instant
from data_engine.datahub.capture_core.universe_plane import UNIVERSE_SOURCES
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

_FROZEN_FACTOR_INPUTS_SQL = (
    "select member.factor_input from staging.core_snapshots snapshot "
    "join staging.core_snapshot_members member on member.snapshot_id = snapshot.snapshot_id "
    "where snapshot.run_id = %s order by member.snapshot_id, member.factor_input ->> 'issuer_id'"
)
_OBSERVATION_FETCH_SQL = (
    "select observation.observation_id, vintage.raw_fetch_id, payload.normalized_payload ->> 'currency' "
    "from staging.capture_normalized_observations observation "
    "join raw.capture_source_vintages vintage on vintage.source_vintage_id = observation.source_vintage_id "
    "left join staging.capture_observation_payloads payload on payload.observation_id = observation.observation_id "
    "where observation.observation_id = any(%s) and vintage.raw_fetch_id is not null"
)
_INSERT_SQL = (
    "insert into mart.oracle_outcomes "
    "(run_id, subject_id, field, period, served_value, oracle_value, oracle_kind, source, observation_id, "
    "knowable_at, ratio, tolerance, result, reason, compared_at) values ("
    "%(run_id)s, %(subject_id)s, %(field)s, %(period)s, %(served_value)s, %(oracle_value)s, %(oracle_kind)s, "
    "%(source)s, %(observation_id)s, %(knowable_at)s, %(ratio)s, %(tolerance)s, %(result)s, %(reason)s, %(compared_at)s) "
    "on conflict on constraint oracle_outcomes_once do nothing"
)


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
    key_expr, val_expr = (
        f"upper(p.normalized_payload ->> '{col}')" if col == "ticker" else f"p.normalized_payload ->> '{col}'"
        for col in (key_col, val_col)
    )
    rows = connection.execute(
        f"select distinct {key_expr}, {val_expr} {CAPTURE_OBSERVATION_JOIN} "  # noqa: S608
        f"where ob.run_id = %s and p.normalized_payload ->> '{key_col}' is not null and p.normalized_payload ->> '{val_col}' is not null",
        (run_id,),
    ).fetchall()
    return {str(key): str(val) for key, val in rows if key and val}


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


def _scalar_result(ratio: Decimal, tolerance: Decimal) -> str:
    return AGREE if abs(ratio - 1) <= tolerance else DISAGREE


def compare_listed_units(
    factor_input: ToptCoreSnapshotInput,
    figures: dict[str, OperatorFigure],
    *,
    ticker: str | None,
    list_as_of: date,
    tolerance: Decimal,
) -> Outcome:
    served = served_listed_units(factor_input)
    figure = figures.get(ticker) if ticker else None
    witness: Decimal | None = None
    ratio: Decimal | None = None
    tol_out: Decimal | None = None
    reason: str | None = None
    res = NOT_COMPARABLE
    if figure is None:
        period, source, row_ref, knowable, reason = (
            list_as_of.isoformat(),
            DataSource.NASDAQ_INDEX.value,
            None,
            None,
            NO_OPERATOR_ROW,
        )
    else:
        period, source, row_ref, knowable = (
            figure.as_of.isoformat(),
            figure.source,
            f"etf-constituent-fact:{figure.row_id}",
            figure.knowable_at,
        )
        reason = (
            NO_MARKET_CAP
            if figure.market_cap is None
            else (NO_PRICE if figure.last_sale_price is None or figure.last_sale_price <= 0 else None)
        )
        if reason is None:
            assert figure.market_cap is not None and figure.last_sale_price is not None
            witness = figure.market_cap / figure.last_sale_price
            if served is None:
                reason = NO_SERVED_VALUE
            else:
                ratio, tol_out = served / witness, tolerance
                res = _scalar_result(ratio, tolerance)
    return Outcome(
        factor_input.issuer_id,
        OPERATOR_FIELD,
        OPERATOR_KIND,
        period,
        served,
        witness,
        source,
        row_ref,
        knowable,
        ratio,
        tol_out,
        res,
        reason,
    )


def operator_figures(
    connection: psycopg.Connection[Any], cutoff: datetime
) -> tuple[dict[str, OperatorFigure], date | None]:
    etfs = [source.etf for source in UNIVERSE_SOURCES.values() if source.index_api]
    figures: dict[str, OperatorFigure] = {}
    newest: date | None = None
    for etf in etfs:
        rows = connection.execute(
            "select id, ticker, as_of, source, market_cap, last_sale_price, knowable_at from staging.etf_constituent_facts "
            "where etf_symbol = %(etf)s and knowable_at <= %(cutoff)s and as_of = ("
            "select max(as_of) from staging.etf_constituent_facts where etf_symbol = %(etf)s and knowable_at <= %(cutoff)s "
            "and market_cap is not null and last_sale_price is not null and last_sale_price > 0) order by id",
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
    connection: psycopg.Connection[Any], run_id: str, cutoff: datetime, factor_inputs: Sequence[ToptCoreSnapshotInput]
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
    witness = None if facts is None else INDEPENDENT_PARSERS[field](facts, cutoff)
    oracle_val: Decimal | None = None
    knowable: datetime | None = None
    ratio: Decimal | None = None
    tol_out: Decimal | None = None
    reason: str | None = None
    res = NOT_COMPARABLE
    if witness is None:
        period, reason = cutoff.isoformat(), NO_DOCUMENT if facts is None else NO_ORACLE_VALUE
    else:
        period, oracle_val, knowable = (
            witness.period_end.isoformat(),
            witness.value,
            knowable_instant(AsOfAxis.ACCESSION, witness.filed),
        )
        reason = (
            NO_SERVED_VALUE
            if served is None
            else UNIT_NOT_USD
            if (currency or getattr(metric, "unit", INDEPENDENT_UNIT)) != INDEPENDENT_UNIT
            else ZERO_ORACLE_VALUE
            if witness.value == 0
            else None
        )
        if reason is None:
            assert served is not None
            ratio, reason = _ratio_and_sign(served, witness.value)
            tol_out = tolerance
            res = DISAGREE if reason else _scalar_result(ratio, tolerance)
    return Outcome(
        factor_input.issuer_id,
        field,
        INDEPENDENT_KIND,
        period,
        served,
        oracle_val,
        DataSource.SEC.value,
        observation_id,
        knowable,
        ratio,
        tol_out,
        res,
        reason,
    )


def stored_document(connection: psycopg.Connection[Any], fetch_id: int) -> dict[str, Any] | None:
    try:
        document = json.loads(raw_store.get_payload(connection, fetch_id))
    except (LookupError, StorageError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _financial_observation(factor_input: ToptCoreSnapshotInput) -> str | None:
    for field in INDEPENDENT_PARSERS:
        if (metric := getattr(factor_input, field)) is not None:
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
            compare_independent_parse(fi, f, facts, cutoff=day, tolerance=tolerance, currency=currency)
            for f in INDEPENDENT_PARSERS
        )
    return outcomes


def canonical_metric(metric: str) -> str:
    return INPUT_KEY_ALIASES.get(metric, metric)


def _score_answer(
    answer: SentinelKnownAnswer, served_value: Decimal, policy_tolerance: Decimal
) -> tuple[Decimal, Decimal, Decimal, str, str | None]:
    if answer.kind == "band":
        lower, upper = Decimal(str(answer.lower_bound)), Decimal(str(answer.upper_bound))
        target = (lower + upper) / Decimal(2)
        tolerance = Decimal(str(answer.relative_tolerance)) if answer.relative_tolerance else (upper - target) / target
    else:
        target = Decimal(str(answer.expected_value))
        tolerance = Decimal(str(answer.relative_tolerance)) if answer.relative_tolerance else policy_tolerance
    ratio, sign_reason = _ratio_and_sign(served_value, target)
    if sign_reason:
        return target, ratio, tolerance, DISAGREE, sign_reason
    if answer.kind == "band":
        result = AGREE if lower <= served_value <= upper else DISAGREE
    elif target == 0:
        result = AGREE if served_value == 0 else DISAGREE
    else:
        result = _scalar_result(ratio, tolerance)
    return target, ratio, tolerance, result, None


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
    reason = (
        SUPERSEDED
        if actual_period_end is not None and str(actual_period_end) > answer.period_end
        else PRIOR_PERIOD
        if actual_period_end is not None and str(actual_period_end) < answer.period_end
        else NO_SERVED_VALUE
        if served_value is None
        else NO_OBSERVATION
        if observation_id is None or knowable_at is None
        else None
    )
    oracle_val: Decimal | None = None
    ratio: Decimal | None = None
    tol_out: Decimal | None = None
    res = NOT_COMPARABLE
    obs_out = None if reason == NO_OBSERVATION else observation_id
    know_out = None if reason == NO_OBSERVATION else knowable_at
    if reason is None:
        assert served_value is not None
        oracle_val, ratio, tol_out, res, reason = _score_answer(answer, served_value, policy_tolerance)
    return Outcome(
        resolved_subject,
        canonical_field,
        FILING_KIND,
        answer.period_end,
        served_value,
        oracle_val,
        source,
        obs_out,
        know_out,
        ratio,
        tol_out,
        res,
        reason,
    )


def _cik_keys(issuer_id: object) -> tuple[str, ...]:
    raw = str(issuer_id)
    return (raw, raw.removeprefix("issuer:cik:")) if raw.startswith("issuer:cik:") else (raw,)


def _query_gppe_by_issuer(
    connection: psycopg.Connection[Any], run_id: str, cutoff: datetime
) -> dict[str, tuple[Decimal | None, str | None, str | None, datetime | None]]:
    rows = connection.execute(
        """
        select distinct on (mnv.issuer_id)
               mnv.issuer_id, mnv.value, cil.operating_period_end, mnv.input_observation_ids, cil.cutoff, cil.result_id
        from mart.metric_node_values mnv
        join mart.capture_issuer_lineage cil using (run_id, issuer_id)
        where mnv.run_id = %(run_id)s
          and (
              (cil.operating_branch = 'financial' and mnv.node_key = %(bank_key)s)
              or mnv.node_key = %(base_key)s
          )
        order by mnv.issuer_id, (mnv.node_key = %(bank_key)s) desc
        """,
        {"run_id": run_id, "bank_key": GPPE_BANKING_TCE_SPEC.root, "base_key": GPPE_V0_SPEC.root},
    ).fetchall()
    gppe_by_issuer: dict[str, tuple[Decimal | None, str | None, str | None, datetime | None]] = {}
    for iss_id, gppe, op_period, obs_ids, cut, res_id in rows:
        period_str = (
            op_period.isoformat()
            if isinstance(op_period, (date, datetime))
            else (str(op_period) if op_period else None)
        )
        entry = (gppe, period_str, obs_ids[0] if obs_ids else res_id, cut or cutoff)
        for key in _cik_keys(iss_id):
            gppe_by_issuer[key] = entry
    return gppe_by_issuer


def _query_fin_by_issuer(
    connection: psycopg.Connection[Any], run_id: str
) -> dict[str, tuple[str, datetime, dict[str, Any]]]:
    rows = connection.execute(
        f"select coalesce(p.normalized_payload ->> 'issuer_id', ''), o.observation_id, o.knowable_at, p.normalized_payload "
        f"{CAPTURE_OBSERVATION_JOIN} where ob.run_id = %s and o.semantic_type = 'financial-fact' order by o.observation_id",
        (run_id,),
    ).fetchall()
    fin_by_issuer: dict[str, tuple[str, datetime, dict[str, Any]]] = {}
    for iss_id, obs_id, know_at, payload in rows:
        for key in _cik_keys(iss_id):
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
    know_at: datetime | None = None
    if canonical_field in _PAYLOAD_PERIOD_KEYS:
        raw, period = payload.get(canonical_field), payload.get(_PAYLOAD_PERIOD_KEYS[canonical_field])
    elif canonical_field == "total_assets":
        raw = payload.get("total_assets")
        period = payload.get("operating_period_end") or payload.get("tangible_equity_period_end")
    elif canonical_field == "employees_total":
        raw = payload.get("employees_total") or payload.get("headcount")
        hc_vintage = (payload.get("vintage") or {}).get("headcount") or {}
        period = (
            hc_vintage.get("period_end") or payload.get("revenue_period_end") or payload.get("operating_period_end")
        )
        if hc_vintage.get("knowable_at"):
            try:
                know_at = datetime.fromisoformat(hc_vintage["knowable_at"])
            except Exception:
                pass
    else:
        return (None, None, None)
    return (None if raw is None else Decimal(str(raw)), period, know_at)


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
    if listing_tickers := run_tickers(connection, run_id):
        for (fi,) in connection.execute(_FROZEN_FACTOR_INPUTS_SQL, (run_id,)).fetchall():
            if (
                isinstance(fi, dict)
                and (lid := fi.get("listing_id"))
                and (iid := fi.get("issuer_id"))
                and lid in listing_tickers
            ):
                issuers.setdefault(listing_tickers[lid], str(iid))
    return issuers


def _first_match(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def known_answer_outcomes(
    connection: psycopg.Connection[Any], run_id: str, cutoff: datetime, factor_inputs: Sequence[ToptCoreSnapshotInput]
) -> list[Outcome]:
    catalog = load_sentinel_known_answers()
    try:
        policy_tolerance = load_oracle_policy().relative_tolerance(KNOWN_ANSWERS_COMPARATOR)
    except (KeyError, ValueError):
        policy_tolerance = Decimal("0.01")
    issuers_by_ticker = run_issuer_tickers(connection, run_id)
    by_issuer_input = {fi.issuer_id: fi for fi in factor_inputs}
    gppe_by_issuer = _query_gppe_by_issuer(connection, run_id, cutoff)
    fin_by_issuer = _query_fin_by_issuer(connection, run_id)
    all_run_issuers = set(by_issuer_input) | set(issuers_by_ticker.values()) | set(fin_by_issuer) | set(gppe_by_issuer)
    all_run_ciks = {
        iss.removeprefix("issuer:cik:")
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
        fi = _first_match(by_issuer_input, target_subject_id, answer.issuer_id, f"issuer:cik:{answer.cik}")
        if canonical_field == "gross_profit_per_employee":
            gppe_entry = _first_match(gppe_by_issuer, target_subject_id, answer.issuer_id, answer.cik)
            if gppe_entry is not None:
                served_val, period, obs_id, know_at = gppe_entry
            elif fi:
                served_val, period, obs_id, know_at = (
                    None,
                    None,
                    fi.observation_ids[0] if fi.observation_ids else None,
                    cutoff,
                )
            else:
                served_val = period = obs_id = know_at = None
        else:
            fin_entry = _first_match(fin_by_issuer, target_subject_id, answer.issuer_id, answer.cik)
            served_val = period = obs_id = know_at = None
            if fin_entry is not None:
                obs_id, know_at, payload = fin_entry
                served_val, period, custom_know = _extract_metric_from_payload(payload, canonical_field)
                if custom_know is not None:
                    know_at = custom_know
            if served_val is None and fi is not None:
                val, fi_obs, fi_know = _extract_metric_from_factor_input(fi, canonical_field)
                if val is not None:
                    served_val, obs_id, know_at = val, obs_id or fi_obs, know_at or fi_know
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
    if (cutoff := fetch_topt_cutoff(connection, run_id)) is None:
        return 0
    factor_inputs = [
        ToptCoreSnapshotInput.model_validate(row[0]) for row in connection.execute(_FROZEN_FACTOR_INPUTS_SQL, (run_id,))
    ]
    outcomes = [
        *operator_outcomes(connection, run_id, cutoff, factor_inputs),
        *independent_parse_outcomes(connection, cutoff, factor_inputs),
        *known_answer_outcomes(connection, run_id, cutoff, factor_inputs),
    ]
    if not outcomes:
        return 0
    connection.execute("set local lock_timeout = '5s'")
    with connection.cursor() as cur:
        cur.executemany(
            _INSERT_SQL,
            [{"run_id": run_id, **dataclasses.asdict(outcome), "compared_at": compared_at} for outcome in outcomes],
        )
        return cur.rowcount


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
    prob = disagrees / compared
    centre = prob + z2 / (2.0 * compared)
    half_width = _WILSON_Z_95 * math.sqrt((prob * (1.0 - prob) + z2 / (4.0 * compared)) / compared)
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
    timestamps = [item["_stamp"] for item in rows if item["_stamp"] is not None]
    return {
        "compared": compared,
        "agree": agree,
        "disagree": disagree,
        "not_comparable": not_comparable,
        "newest_compared_at": max(timestamps) if timestamps else None,
        "wilson_95_upper_bound": wilson_95_upper_bound(disagree, compared),
    }


def _keep_latest(bucket: dict[Any, dict[str, Any]], key: Any, item: dict[str, Any], stamp: str | None) -> None:
    existing = bucket.get(key)
    if existing is None or (stamp or "") >= (existing["_stamp"] or ""):
        bucket[key] = item


def _grouped_summary(items: Sequence[dict[str, Any]], key_name: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(str(item[key_name]), []).append(item)
    return {key: _summarize_counts(rows) for key, rows in sorted(grouped.items())}


def rollup_oracle_outcomes(rows: Sequence[Mapping[str, Any] | Outcome]) -> dict[str, Any]:
    deduped: dict[tuple[str | None, str, str], dict[str, Any]] = {}
    by_comp: dict[tuple[str, str | None, str, str], dict[str, Any]] = {}
    for row in rows:
        obs_id = _row_attr(row, "observation_id")
        obs_id_str = None if obs_id is None else str(obs_id)
        sid, fld = str(_row_attr(row, "subject_id", "")), str(_row_attr(row, "field", ""))
        okind = _row_attr(row, "oracle_kind")
        okind_str = None if okind is None else str(okind)
        stamp = _iso_utc(_row_attr(row, "compared_at"))
        reason = _row_attr(row, "reason")
        norm = {
            "observation_id": obs_id_str,
            "subject_id": sid,
            "field": fld,
            "result": str(_row_attr(row, "result", "")),
            "reason": None if reason is None else str(reason),
            "oracle_kind": okind_str,
            "_stamp": stamp,
        }
        key = (obs_id_str, sid, fld)
        _keep_latest(deduped, key, norm, stamp)
        if okind_str:
            _keep_latest(by_comp, (okind_str, *key), norm, stamp)
    drows, cvals = list(deduped.values()), list(by_comp.values())
    comparators = {
        kind: summary
        for kind, summary in _grouped_summary(cvals, "oracle_kind").items()
        if not (
            kind == "independent_parse" and all(r["reason"] == "no_document" for r in cvals if r["oracle_kind"] == kind)
        )
    }
    return {**_summarize_counts(drows), "by_field": _grouped_summary(drows, "field"), "comparators": comparators}


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
        return [dict(zip(cols, row, strict=True)) for row in rows]
    except Exception:
        return []


def load_oracle_outcomes_rollup(connection: psycopg.Connection[Any], *, run_id: str | None = None) -> dict[str, Any]:
    cols = ("observation_id", "subject_id", "field", "result", "reason", "oracle_kind", "compared_at")
    return rollup_oracle_outcomes(
        _query_oracle_rows(
            connection, cols, "where run_id = %s" if run_id is not None else "", (run_id,) if run_id is not None else ()
        )
    )


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
    if oracle_kind == OPERATOR_KIND or canonical_metric(str(row.get("field") or "")) == "gross_profit_per_employee":
        return None
    for key in ("origin_id", "lineage", "origin"):
        if isinstance(explicit := row.get(key), str) and explicit.startswith("origin:"):
            return explicit
    source = str(row.get("source") or "")
    if source.startswith("origin:"):
        return source
    lowered = source.lower()
    for token, origin_id in _SOURCE_SUBSTRING_ORIGINS:
        if token in lowered:
            return origin_id
    return "origin:sec-company-facts:v1" if oracle_kind in (FILING_KIND, INDEPENDENT_KIND) else None


def _dedupe_rows_by_cutoff(
    rows: Sequence[Mapping[str, Any] | Outcome],
    cutoff: datetime | None,
    key_fn: Callable[[dict[str, Any]], tuple[str, ...] | None],
) -> list[dict[str, Any]]:
    cutoff_iso = (_iso_utc(cutoff) or "") if cutoff is not None else None
    deduped: dict[tuple[str, ...], dict[str, Any]] = {}
    for raw in rows:
        item = _outcome_to_dict(raw)
        stamp = _iso_utc(item.get("compared_at")) or _iso_utc(item.get("knowable_at"))
        if (cutoff_iso is not None and stamp is not None and stamp > cutoff_iso) or (key := key_fn(item)) is None:
            continue
        item["_stamp"] = stamp or ""
        _keep_latest(deduped, key, item, stamp)
    return list(deduped.values())


_OUTCOME_MASS_DELTAS: dict[tuple[str, str], tuple[Decimal, Decimal]] = {
    (FILING_KIND, AGREE): (Decimal("1"), Decimal("0")),
    (FILING_KIND, DISAGREE): (Decimal("0"), Decimal("1")),
}


def oracle_outcome_mass_by_origin(
    rows: Sequence[Mapping[str, Any] | Outcome], *, cutoff: datetime | None = None
) -> dict[str, tuple[Decimal, Decimal]]:
    def _key(item: dict[str, Any]) -> tuple[str, ...] | None:
        oracle_kind, subject_id = str(item.get("oracle_kind") or ""), str(item.get("subject_id") or "")
        field = canonical_metric(str(item.get("field") or ""))
        return (
            (oracle_kind, str(item.get("observation_id") or ""), subject_id, field)
            if oracle_kind and subject_id and field
            else None
        )

    totals: dict[str, tuple[Decimal, Decimal]] = {}
    for item in _dedupe_rows_by_cutoff(rows, cutoff, _key):
        if (origin_id := _resolve_row_origin_id(item)) is None:
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
    if (ratio_raw := row.get("ratio")) is None:
        return None
    ratio_val = Decimal(str(ratio_raw))
    return (
        ratio_val if (result == AGREE and Decimal("0") <= ratio_val < Decimal("0.5")) else abs(ratio_val - Decimal("1"))
    )


def _agreement_from_outcome_row(row: Mapping[str, Any]) -> Decimal:
    result = str(row.get("result") or "")
    if str(row.get("reason") or "") == SIGN_DIFFERS:
        return Decimal("0.25")
    if (delta := _relative_delta_from_row(row, result)) is None:
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
    targets = {subject_ids} if isinstance(subject_ids, str) else {sid for sid in subject_ids if sid}
    for sid in list(targets):
        if sid.startswith("issuer:cik:"):
            targets.add(sid.removeprefix("issuer:cik:"))
        elif sid.isdigit():
            targets.add(f"issuer:cik:{sid.zfill(10)}")
    canonical_field = canonical_metric(field)
    candidate_fields = {canonical_field, field, *(("headcount",) if canonical_field == "employees_total" else ())}

    def _key(item: dict[str, Any]) -> tuple[str, ...] | None:
        subj, row_field = str(item.get("subject_id") or ""), str(item.get("field") or "")
        if subj not in targets or (
            row_field not in candidate_fields and canonical_metric(row_field) not in candidate_fields
        ):
            return None
        return (str(item.get("oracle_kind") or "oracle"), subj, canonical_field)

    deduped = _dedupe_rows_by_cutoff(rows, cutoff, _key)
    comparable = [item for item in deduped if str(item.get("result") or "") in (AGREE, DISAGREE)]
    if not comparable:
        return Decimal("1"), ("oracle:none",)
    reasons = {"oracle:agree" if str(item.get("result") or "") == AGREE else "oracle:disagree" for item in comparable}
    return min(_agreement_from_outcome_row(item) for item in comparable), tuple(sorted(reasons))


_ORACLE_CONFIDENCE_COLS = (
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


def load_oracle_confidence_inputs(
    connection: psycopg.Connection[Any], *, run_id: str | None = None, cutoff: datetime | None = None
) -> list[dict[str, Any]]:
    where, params = (
        ("where compared_at <= %s", (cutoff,))
        if cutoff is not None
        else (("where run_id = %s", (run_id,)) if run_id is not None else ("", ()))
    )
    return _query_oracle_rows(connection, _ORACLE_CONFIDENCE_COLS, where, params)
