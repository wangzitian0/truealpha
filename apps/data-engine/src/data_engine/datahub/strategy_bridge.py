"""The capture -> strategy bridge (#429)."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import cache, partial
from typing import Any

import psycopg
from factors.composite.strategy_evaluator import IssuerInput, Measure, evaluate_cutoff, required_input_keys
from factors.panel import IssuerClass
from truealpha_contracts.fiscal_period import FiscalPeriod as AnnualFiscalPeriod
from truealpha_contracts.metrics import is_registered_input_key
from truealpha_contracts.strategy import ExclusionReason, LargeModelValueV0Definition, load_live_strategy_definition
from truealpha_runtime.storage import StorageError

from data_engine import raw_store
from data_engine.core_strategy_replay import _to_decision
from data_engine.datahub.input_cells import InputCell, financial_input_rows, last_close_cell, live_knowable_at
from data_engine.datahub.market_prices import SPLIT_ADJUSTED, UNADJUSTED
from data_engine.datahub.price_series import load_daily_series
from data_engine.datahub.primary_listing import choose_primary_listings
from data_engine.datahub.production_topt.concept_mapping import resolve_ruleset
from data_engine.datahub.production_topt.executor import SERVED_BY_FAILOVER
from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION as PRIMARY_PARSER_VERSION
from data_engine.datahub.risk_free import RiskFreeRate
from data_engine.datahub.share_basis import (
    SHARES_KEY,
    LiveShareCount,
    OnCloseBasis,
    Unavailable,
    listing_units_per_share_of,
    live_on_close_basis,
    on_close_basis,
    split_history,
)
from data_engine.datahub.share_count_entry import share_count_entry
from data_engine.strategy_backtest_gateway import StrategyBacktestGateway
from data_engine.strategy_replay_repository import bind_strategy_run_to_capture, write_replay

log = logging.getLogger(__name__)

_Cell = tuple[str, dict, datetime]

_RUN_LISTINGS_SQL = """
select distinct on (p.normalized_payload ->> 'listing_id')
       p.normalized_payload ->> 'listing_id',
       p.normalized_payload ->> 'issuer_id'
from raw.capture_obligations ob
join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
join staging.capture_observation_payloads p on p.observation_id = o.observation_id
where ob.run_id = %s
  and o.semantic_type in ('listing-identity', 'financial-fact')
order by p.normalized_payload ->> 'listing_id',
         (o.semantic_type = 'listing-identity') desc,
         p.normalized_payload ->> 'issuer_id'
"""

_LISTING_TICKERS_SQL = """
select distinct p.normalized_payload ->> 'issuer_id', p.normalized_payload ->> 'ticker'
from raw.capture_obligations ob
join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
join staging.capture_observation_payloads p on p.observation_id = o.observation_id
where ob.run_id = %s
  and o.semantic_type = 'listing-identity'
  and p.normalized_payload ->> 'issuer_id' = any(%s)
  and p.normalized_payload ->> 'ticker' is not null
"""

_FINANCIAL_SOURCES_SQL = """
select distinct v.raw_fetch_id
from raw.capture_obligations ob
join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
join staging.capture_observation_payloads p on p.observation_id = o.observation_id
join raw.capture_source_vintages v on v.source_vintage_id = o.source_vintage_id
where ob.run_id = %s
  and o.semantic_type = 'financial-fact'
  and o.parser_version = %s
  and (p.normalized_payload ->> 'issuer_id' = %s or p.normalized_payload ->> 'listing_id' = %s)
  and v.raw_fetch_id is not null
order by 1
"""


def load_strategy_definition() -> LargeModelValueV0Definition:
    return load_live_strategy_definition()


def _listings_by_issuer(listing_to_issuer: Mapping[str, str]) -> dict[str, list[str]]:
    by_issuer: dict[str, list[str]] = {}
    for listing_id, issuer_id in listing_to_issuer.items():
        by_issuer.setdefault(issuer_id, []).append(listing_id)
    return by_issuer


def seed_strategy_inputs_from_capture(
    connection: psycopg.Connection[Any],
    run_id: str,
    *,
    cutoff: datetime,
    parser_version: str = PRIMARY_PARSER_VERSION,
) -> int:
    raw_rows = connection.execute(
        """
        select o.semantic_type, o.confidence, p.normalized_payload, o.knowable_at
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        where ob.run_id = %s
          and o.semantic_type in ('financial-fact', 'market-price', 'listing-identity')
          and (
              o.parser_version = %s
              or o.semantic_type = 'listing-identity'
              or (o.semantic_type = 'market-price' and p.normalized_payload ->> %s is not null)
          )
        """,
        (run_id, parser_version, SERVED_BY_FAILOVER),
    ).fetchall()
    rows = [(r[0], None, r[1], r[2]) if len(r) == 3 else (r[0], r[1], r[2], r[3]) for r in raw_rows]

    listing_to_issuer: dict[str, str] = {
        str(payload["listing_id"]): str(payload["issuer_id"])
        for semantic_type, _conf, payload, _obs_at in rows
        if semantic_type == "listing-identity"
    }
    for semantic_type, _conf, payload, _obs_at in rows:
        if semantic_type in ("financial-fact", "market-price"):
            listing_to_issuer.setdefault(str(payload["listing_id"]), str(payload["issuer_id"]))
    primary = choose_primary_listings(connection, _listings_by_issuer(listing_to_issuer), known_at=cutoff)

    from data_engine.datahub.confidence_report import evaluate_and_persist_run_cell_confidence

    cell_confidences = evaluate_and_persist_run_cell_confidence(connection, run_id, cutoff=cutoff)

    financial: dict[str, tuple[str, Decimal | None, dict, datetime]] = {}
    price: dict[str, tuple[str, Decimal | None, dict, datetime]] = {}
    for semantic_type, raw_conf, payload, observed_at in rows:
        if semantic_type == "listing-identity":
            continue
        listing_id = str(payload["listing_id"])
        issuer_id = listing_to_issuer.get(listing_id, str(payload["issuer_id"]))
        bucket = financial if semantic_type == "financial-fact" else price
        if primary.get(issuer_id) == listing_id and issuer_id not in bucket:
            bucket[issuer_id] = (
                listing_id,
                Decimal(str(raw_conf)) if raw_conf is not None else None,
                payload,
                observed_at,
            )

    on_basis = _counts_on_close_basis(
        connection,
        run_id,
        {k: (v[0], v[2], v[3]) for k, v in financial.items()},
        {k: (v[0], v[2], v[3]) for k, v in price.items()},
        parser_version=parser_version,
    )
    written = 0
    for issuer_id in sorted(set(financial) | set(price)):
        inputs: list[InputCell] = []
        if issuer_id in financial:
            fin_listing, fallback_conf, payload, observed_at = financial[issuer_id]
            listing_confs = {metric: conf for (subj, metric), conf in cell_confidences.items() if subj == fin_listing}
            inputs.extend(
                _on_close_basis_rows(
                    financial_input_rows(payload, fallback_conf, observed_at, confidence_by_key=listing_confs),
                    on_basis.get(issuer_id),
                )
            )
        if issuer_id in price:
            price_listing, fallback_price_conf, payload, observed_at = price[issuer_id]
            fin_currency = financial[issuer_id][2].get("currency") if issuer_id in financial else None
            close_conf = cell_confidences.get((price_listing, "close"), fallback_price_conf)
            if (fin_currency is None or payload.get("currency") == fin_currency) and (
                close_cell := last_close_cell(payload, close_conf, observed_at)
            ) is not None:
                inputs.append(close_cell)
        for input_key, value, _unit, confidence, knowable_at, fiscal_period in inputs:
            if not is_registered_input_key(input_key):
                raise ValueError(
                    f"{input_key!r} is not a registered metric (truealpha_contracts.metrics.METRICS); "
                    "register it there before seeding it as a strategy input"
                )
            connection.execute(
                """
                insert into staging.strategy_backtest_inputs
                    (issuer_id, cutoff_at, input_key, value, confidence, knowable_at, fiscal_period)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (issuer_id, cutoff, input_key, value, confidence, knowable_at, fiscal_period),
            )
            written += 1
    return written


def undeclared_primary_listing_issuers(
    connection: psycopg.Connection[Any], run_id: str, *, cutoff: datetime
) -> tuple[str, ...]:
    listing_to_issuer = {
        str(listing_id): str(issuer_id)
        for listing_id, issuer_id in connection.execute(_RUN_LISTINGS_SQL, (run_id,)).fetchall()
    }
    chosen = choose_primary_listings(connection, _listings_by_issuer(listing_to_issuer), known_at=cutoff)
    return tuple(sorted(issuer_id for issuer_id, listing_id in chosen.items() if listing_id is None))


def _counts_on_close_basis(
    connection: psycopg.Connection[Any],
    run_id: str,
    financial: Mapping[str, _Cell],
    price: Mapping[str, _Cell],
    *,
    parser_version: str,
) -> dict[str, OnCloseBasis | Unavailable]:
    meeting = sorted(
        issuer_id
        for issuer_id in financial.keys() & price.keys()
        if financial[issuer_id][1].get(SHARES_KEY) is not None and price[issuer_id][1].get("close") is not None
    )
    outcomes: dict[str, OnCloseBasis | Unavailable] = {}
    for issuer_id, (_listing, payload, observed_at) in financial.items():
        shares_raw = payload.get(SHARES_KEY)
        if shares_raw is None or issuer_id in meeting:
            continue
        knowable = live_knowable_at(payload, observed_at)(SHARES_KEY, None)
        filed_day = knowable.astimezone(UTC).date()
        outcomes[issuer_id] = on_close_basis(
            Decimal(str(shares_raw)),
            knowable,
            basis_date=filed_day,
            close_session=filed_day,
            close_knowable_at=knowable,
            splits=(),
            listing_units_per_share=listing_units_per_share_of(payload),
            shares_basis=payload.get("shares_basis"),
        )
    if not meeting:
        return outcomes
    tickers: dict[str, list[str]] = {}
    for issuer_id, ticker in connection.execute(_LISTING_TICKERS_SQL, (run_id, meeting)).fetchall():
        tickers.setdefault(str(issuer_id), []).append(str(ticker))
    symbols = sorted({ticker for names in tickers.values() for ticker in names})
    unadjusted = load_daily_series(connection, symbols, adjust=UNADJUSTED)
    split_adjusted = load_daily_series(connection, symbols, adjust=SPLIT_ADJUSTED)
    histories = {symbol: split_history(symbol, unadjusted, split_adjusted) for symbol in symbols}
    ruleset = cache(partial(resolve_ruleset, connection))
    for issuer_id in meeting:
        fin_listing, payload, observed_at = financial[issuer_id]
        _price_listing, close_payload, close_observed_at = price[issuer_id]
        names = sorted(tickers.get(issuer_id, ()))
        outcome = live_on_close_basis(
            LiveShareCount(
                shares=Decimal(str(payload[SHARES_KEY])),
                knowable_at=live_knowable_at(payload, observed_at)(SHARES_KEY, None),
                entry=share_count_entry(payload.get("vintage"), payload.get("shares_period_end"), observed_at),
                close_session=close_observed_at.astimezone(UTC).date(),
                close_knowable_at=close_observed_at,
                close=Decimal(str(close_payload["close"])),
            ),
            [histories[symbol] for symbol in names],
            listing_units_per_share=listing_units_per_share_of(payload),
            shares_basis=payload.get("shares_basis"),
            documents=partial(
                _company_facts_documents, connection, run_id, issuer_id, parser_version, listing_id=fin_listing
            ),
            ruleset=ruleset,
        )
        if isinstance(outcome, Unavailable):
            log.warning(
                "share basis: %s (%s) gets no share row beside the close %s: %s: %s",
                issuer_id,
                ", ".join(names) or "no listing ticker",
                close_payload.get("close"),
                outcome.reason,
                outcome.detail,
            )
        elif outcome.factor != 1:
            log.info(
                "share basis: %s (%s) filed count %s multiplied by %s for splits before the close",
                issuer_id,
                ", ".join(names),
                payload[SHARES_KEY],
                outcome.factor,
            )
        outcomes[issuer_id] = outcome
    return outcomes


def _company_facts_documents(
    connection: psycopg.Connection[Any],
    run_id: str,
    issuer_id: str,
    parser_version: str,
    *,
    listing_id: str = "",
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for (raw_fetch_id,) in connection.execute(
        _FINANCIAL_SOURCES_SQL, (run_id, parser_version, issuer_id, listing_id or issuer_id)
    ).fetchall():
        try:
            documents.append(json.loads(raw_store.get_payload(connection, raw_fetch_id)))
        except (LookupError, StorageError, ValueError) as error:
            log.warning("share basis: raw.fetches:%s of %s cannot be read: %s", raw_fetch_id, issuer_id, error)
    return documents


def _on_close_basis_rows(rows: Sequence[InputCell], outcome: OnCloseBasis | Unavailable | None) -> list[InputCell]:
    if outcome is None:
        return list(rows)
    kept: list[InputCell] = []
    for cell in rows:
        if cell.input_key != SHARES_KEY or cell.fiscal_period is not None:
            kept.append(cell)
        elif isinstance(outcome, OnCloseBasis):
            kept.append(
                cell
                if (outcome.value, outcome.knowable_at) == (cell.value, cell.knowable_at)
                else cell._replace(value=outcome.value, knowable_at=outcome.knowable_at)
            )
    return kept


def run_strategy_replay_for_cutoff(
    connection: psycopg.Connection[Any],
    *,
    cutoff: datetime,
    executed_at: datetime,
    risk_free_rate: RiskFreeRate,
    capture_run_id: str,
    plausibility_refusals: Mapping[str, ExclusionReason],
) -> tuple[str, int, str]:
    definition = load_strategy_definition()
    gateway = StrategyBacktestGateway(connection)
    issuer_classes = _issuer_classes_for_run(connection, capture_run_id)
    periods_by_issuer = _issuer_periods_for_run(connection, capture_run_id)
    issuer_inputs = [
        _with_periods(
            replace(item, issuer_class=issuer_classes.get(item.issuer_id)),
            periods_by_issuer.get(item.issuer_id, {}),
        )
        for item in gateway.issuer_inputs(cutoff)
    ]
    seeded = {item.issuer_id for item in issuer_inputs}
    issuer_inputs += [
        IssuerInput(issuer_id=issuer_id, records={}, knowable_at={})
        for issuer_id in sorted(plausibility_refusals)
        if issuer_id not in seeded
    ]
    evaluated = evaluate_cutoff(
        issuer_inputs,
        definition=definition,
        cutoff_at=cutoff,
        risk_free_rate=risk_free_rate.value,
        plausibility_refusals=plausibility_refusals,
    )
    cutoff_key = cutoff.astimezone(UTC).isoformat()
    decisions = sorted(
        (_to_decision(item, cutoff_key) for item in evaluated),
        key=lambda item: (item.cutoff_at, item.issuer_id),
    )
    snapshot_id = gateway.snapshot_id(cutoff)
    run_id, decision_ids = write_replay(
        connection,
        decisions,
        definition,
        executed_at=executed_at,
        snapshot_id=snapshot_id,
        risk_free_rate=risk_free_rate,
    )
    bind_strategy_run_to_capture(connection, strategy_run_id=run_id, capture_run_id=capture_run_id)
    return run_id, len(decision_ids), snapshot_id


def _with_periods(issuer: IssuerInput, periods: Mapping[str, AnnualFiscalPeriod]) -> IssuerInput:
    if not periods:
        return issuer
    records = dict(issuer.records)
    for key, period in periods.items():
        if key in records:
            measure, conf = records[key]
            if isinstance(measure, Measure):
                records[key] = (replace(measure, period=period), conf)
    return replace(issuer, records=records)


def _financial_payload_fields_for_run(
    connection: psycopg.Connection[Any], run_id: str, *fields: str
) -> list[tuple[Any, ...]]:
    projections = ", ".join(f"p.normalized_payload ->> '{field}'" for field in fields)
    return connection.execute(
        f"""
        with listing_issuers as (
            select distinct p.normalized_payload ->> 'listing_id' as listing_id,
                            p.normalized_payload ->> 'issuer_id' as issuer_id
            from raw.capture_obligations ob
            join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
            join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
            join staging.capture_observation_payloads p on p.observation_id = o.observation_id
            where ob.run_id = %s and o.semantic_type = 'listing-identity'
        )
        select distinct coalesce(li.issuer_id, p.normalized_payload ->> 'issuer_id'), {projections}
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        left join listing_issuers li on li.listing_id = p.normalized_payload ->> 'listing_id'
        where ob.run_id = %s and o.semantic_type = 'financial-fact'
        """,  # noqa: S608
        (run_id, run_id),
    ).fetchall()


def _annual_period(raw_end: Any) -> AnnualFiscalPeriod:
    end = date.fromisoformat(str(raw_end))
    return AnnualFiscalPeriod(filing_fiscal_year=end.year, kind="FY", start=end - timedelta(days=364), end=end)


def _issuer_periods_for_run(
    connection: psycopg.Connection[Any], run_id: str
) -> dict[str, dict[str, AnnualFiscalPeriod]]:
    result: dict[str, dict[str, AnnualFiscalPeriod]] = {}
    for issuer_id, op_end_raw, rev_end_raw in _financial_payload_fields_for_run(
        connection, run_id, "operating_period_end", "revenue_period_end"
    ):
        if not issuer_id:
            continue
        periods = result.setdefault(str(issuer_id), {})
        if op_end_raw:
            fp = _annual_period(op_end_raw)
            periods["gross_profit"] = fp
            periods["pre_provision_profit"] = fp
        if rev_end_raw:
            periods["revenue"] = _annual_period(rev_end_raw)
    return result


def _issuer_classes_for_run(connection: psycopg.Connection[Any], run_id: str) -> dict[str, IssuerClass]:
    classes: dict[str, IssuerClass] = {}
    for issuer_id, branch in _financial_payload_fields_for_run(connection, run_id, "operating_branch"):
        if branch is None:
            continue
        issuer_class = IssuerClass(branch)
        if classes.setdefault(issuer_id, issuer_class) != issuer_class:
            raise ValueError(f"{issuer_id}: two operating branches in capture run {run_id}")
    return classes


def persist_strategy_input_coverage(
    connection: psycopg.Connection[Any], run_id: str, *, cutoff: datetime
) -> tuple[int, int]:
    definition = load_strategy_definition()
    classes = _issuer_classes_for_run(connection, run_id)
    issuers = [
        row[0]
        for row in connection.execute(
            "select distinct issuer_id from mart.topt_core_meta_info where run_id = %s order by 1", (run_id,)
        )
    ]
    present_rows = connection.execute(
        """
        select distinct on (issuer_id, input_key) issuer_id, input_key
        from staging.strategy_backtest_inputs
        where cutoff_at = %s
        order by issuer_id, input_key, recorded_at desc
        """,
        (cutoff,),
    ).fetchall()
    present: dict[str, set[str]] = {}
    for issuer_id, input_key in present_rows:
        present.setdefault(issuer_id, set()).add(input_key)

    complete = 0
    for issuer_id in issuers:
        required = sorted(required_input_keys(definition, classes.get(issuer_id)))
        missing = [key for key in required if key not in present.get(issuer_id, set())]
        if not missing:
            complete += 1
        connection.execute(
            "insert into mart.strategy_input_coverage "
            "(run_id, issuer_id, required_count, present_count, missing_keys) "
            "values (%s, %s, %s, %s, %s) on conflict (run_id, issuer_id) do nothing",
            (run_id, issuer_id, len(required), len(required) - len(missing), missing),
        )
    return complete, len(issuers)
