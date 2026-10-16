"""The capture → strategy bridge (#429).

Two steps the deployed tick runs after a capture materializes:

* ``seed_strategy_inputs_from_capture`` lands the captured cells' provenance-neutral
  factor inputs into ``staging.strategy_backtest_inputs``, replacing the golden-fixture
  seeding that previously fed the deployed canary.
* ``run_strategy_replay_for_cutoff`` drives the single-source ``strategy_evaluator`` over
  the ``StrategyBacktestGateway`` for that cutoff and persists ``mart.strategy_runs`` /
  ``mart.strategy_decisions``.

The only packaged object read here is the strategy DEFINITION — versioned strategy
configuration, not input data. Golden inputs, decisions and rates are never read (#429
invariant I2). Since #1235 it is the live definition 1.0.0 (P/GP bands), the same one the
TOPT core engine reads.

The seed is where a filed share count meets the captured close that the evaluator multiplies
it by. Since #1262 it puts the count on the share basis of that close first
(``share_basis.live_on_close_basis``), with the rule the history projector applies.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import cache, partial
from typing import Any

import psycopg
from factors.composite.strategy_evaluator import evaluate_cutoff
from factors.forest import IssuerClass
from truealpha_contracts.fiscal_period import encode_annual
from truealpha_contracts.metrics import METRICS, input_key_for_metric, is_registered_input_key
from truealpha_contracts.strategy import LargeModelValueV0Definition, load_live_strategy_definition
from truealpha_runtime.storage import StorageError

from data_engine import raw_store
from data_engine.core_strategy_replay import _to_decision
from data_engine.datahub.canonical_issuer import listing_sort_key
from data_engine.datahub.market_prices import SPLIT_ADJUSTED, UNADJUSTED
from data_engine.datahub.price_series import load_daily_series
from data_engine.datahub.production_topt.concept_mapping import resolve_ruleset
from data_engine.datahub.production_topt.executor import SERVED_BY_FAILOVER
from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION as PRIMARY_PARSER_VERSION
from data_engine.datahub.share_basis import (
    SHARES_KEY,
    LiveShareCount,
    OnCloseBasis,
    Unavailable,
    live_on_close_basis,
    split_history,
)
from data_engine.strategy_backtest_gateway import StrategyBacktestGateway
from data_engine.strategy_replay_repository import bind_strategy_run_to_capture, write_replay

log = logging.getLogger(__name__)

# Every registered metric except `price` surfaces here as the scalar half of its
# financial-fact input -- one row with `fiscal_period` NULL, via the registry's own
# spelling of the metric (init.md rule 22, #770 finding 2) -- adding metric N+1 to
# `truealpha_contracts.metrics.METRICS` needs no edit here. A PERIODIC metric such as
# `net_income` still belongs in this tuple too: this is what lands its latest value as a
# point-in-time scalar (mirroring pre-0043 behavior), while `_STRATEGY_PERIODIC_KEYS`
# below separately lands its whole annual series. `price` is excluded because it never
# travels through the financial-fact payload this loop reads: it has its own bucket
# keyed by `close` below, seeded as `last_close` for the strategy vocabulary. A metric
# with no field on `FinancialFactPayload` yet (`cost_of_revenue`, `operating_income`,
# `eps_diluted`) simply reads back `None` from `payload.get(...)` and is skipped, same as
# any other missing field -- registering it here needs a payload field, not a change to
# this tuple.
_STRATEGY_FINANCIAL_KEYS = tuple(sorted(input_key_for_metric(name) for name in METRICS if name != "price"))

# Which metrics are period-shaped comes from the registry, never from a list here:
# init.md rule 22 forbids generic transport branching on record type.
_STRATEGY_PERIODIC_KEYS = {name: f"{name}_by_period" for name, spec in METRICS.items() if spec.periodic}


def load_strategy_definition() -> LargeModelValueV0Definition:
    """The live large_model_value_v0 strategy definition (1.0.0, #1235). Versioned strategy
    CONFIGURATION (formula constants and bands) — the deployed job never reads the
    golden inputs, decisions, or rates (#429 invariant I2). One reader serves both engines:
    the TOPT core materialization takes its bands from the same function."""
    return load_live_strategy_definition()


def financial_input_rows(
    payload: Mapping[str, Any],
    confidence: Decimal,
    observed_at: datetime,
    *,
    knowable_at_of: Callable[[str, str | None], datetime | None] | None = None,
) -> list[tuple[str, str, Decimal, datetime, str | None]]:
    """The strategy input rows one financial-fact payload yields.

    Each row is ``(input_key, value, confidence, knowable_at, fiscal_period)``. The live writer
    and the historical projector (``datahub.strategy_history``) share this function, so both
    write the same keys, the same period tags and the same confidence gate.

    The live writer stamps every row with the observation's own ``observed_at``. The projector
    passes ``knowable_at_of(input_key, period_end)``, which returns the filing time of one
    input. ``period_end`` is None for a scalar input. A None result drops the row: an input
    without a filing time has no knowable time, and nothing may invent one.
    """
    rows: list[tuple[str, str, Decimal, datetime, str | None]] = []

    def knowable_at(input_key: str, period_end: str | None) -> datetime | None:
        return observed_at if knowable_at_of is None else knowable_at_of(input_key, period_end)

    op_end = payload.get("operating_period_end")
    rev_end = payload.get("revenue_period_end")
    if op_end is not None and rev_end is not None and op_end != rev_end:
        confidence = Decimal("0.00")
    for key in _STRATEGY_FINANCIAL_KEYS:
        value = payload.get(key)
        if value is not None and (stamp := knowable_at(key, None)) is not None:
            rows.append((key, value, confidence, stamp, None))
    for key, payload_key in _STRATEGY_PERIODIC_KEYS.items():
        # The period tag mirrors staging's own encoding so `factors.base.peg`
        # parses one shape wherever the series came from. Only the end date is
        # known here, and an annual period's start is its end less a year; the
        # factor re-checks the duration floor rather than trusting the tag.
        for period_end, value in sorted((payload.get(payload_key) or {}).items()):
            if (stamp := knowable_at(key, period_end)) is None:
                continue
            # One encoder, shared with the factor that parses it (init.md Section 6).
            # Building the tag here and matching it with a regex there is how a
            # format drifts into an empty series instead of an error.
            tag = encode_annual(date.fromisoformat(period_end))
            rows.append((key, value, confidence, stamp, tag))
    return rows


def seed_strategy_inputs_from_capture(
    connection: psycopg.Connection[Any],
    run_id: str,
    *,
    cutoff: datetime,
    parser_version: str = PRIMARY_PARSER_VERSION,
) -> int:
    """Land the run's captured cells as provenance-neutral strategy inputs.

    One canonical listing per issuer (lowest listing_id) supplies ``last_close``; the
    SEC shares figure is issuer-level, so a dual-class issuer's market value uses its
    canonical class's price — an approximation already reflected in the price
    confidence. Missing fields are simply not seeded; the evaluator excludes those
    issuers with explicit reasons rather than receiving fabricated values.

    ``parser_version`` selects which parser vintage of the run's observations crosses the
    bridge; the deployed tick uses the live default, and the #395 end-to-end test drives
    the integration-corpus vintage through the same code. A price cell the primary could
    not serve crosses under the vintage of the origin that served it (#862): its payload
    declares ``served_by_failover``, which no corroboration ever carries, and it is the
    value the snapshot bound for that cell.

    The evaluator multiplies the seeded share count by the seeded close. So the seed puts the
    filed count on the share basis of that close first (#1262, ``_counts_on_close_basis``).
    When the stored series cannot prove the splits between the two, the seed writes no share
    row for the issuer and logs the named reason. The evaluator then excludes the issuer with
    ``missing_market_value_input``. The filed count never stands beside a close of another basis.
    """
    rows = connection.execute(
        """
        select o.semantic_type, o.confidence, p.normalized_payload, o.knowable_at
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        where ob.run_id = %s
          and o.semantic_type in ('financial-fact', 'market-price')
          and (
              o.parser_version = %s
              or (o.semantic_type = 'market-price' and p.normalized_payload ->> %s is not null)
          )
        """,
        (run_id, parser_version, SERVED_BY_FAILOVER),
    ).fetchall()

    # issuer -> (canonical listing, payload, confidence, the observation's own knowable_at)
    financial: dict[str, tuple[str, dict, Decimal, datetime]] = {}
    price: dict[str, tuple[str, dict, Decimal, datetime]] = {}
    for semantic_type, confidence, payload, observed_at in rows:
        issuer_id, listing_id = payload["issuer_id"], payload["listing_id"]
        bucket = financial if semantic_type == "financial-fact" else price
        current = bucket.get(issuer_id)
        if current is None or listing_sort_key(listing_id) < listing_sort_key(current[0]):
            bucket[issuer_id] = (listing_id, payload, Decimal(str(confidence)), observed_at)

    on_basis = _counts_on_close_basis(connection, run_id, financial, price, parser_version=parser_version)
    written = 0
    for issuer_id in sorted(set(financial) | set(price)):
        # `knowable_at` is the observation's OWN knowable time, not a constant offset from
        # the cutoff. It used to be `cutoff - 58 minutes`, which satisfies the table's
        # `knowable_at <= cutoff_at` CHECK for every row while saying nothing about when
        # the data actually became public — so a replay at a historical cutoff could
        # consume a figure that was not knowable then and no constraint would object. That
        # matters most for a derived multi-period input like `earnings_cagr_3y`, whose
        # basis spans several filings (#284).
        inputs: list[tuple[str, str, Decimal, datetime, str | None]] = []
        if issuer_id in financial:
            _listing, payload, confidence, observed_at = financial[issuer_id]
            inputs.extend(
                _on_close_basis_rows(financial_input_rows(payload, confidence, observed_at), on_basis.get(issuer_id))
            )
        if issuer_id in price:
            _listing, payload, confidence, observed_at = price[issuer_id]
            close = payload.get("close")
            if close is not None:
                inputs.append(("last_close", close, confidence, observed_at, None))
        for input_key, value, confidence, knowable_at, fiscal_period in inputs:
            # Registry-backed, not a CHECK: migration 0032's enumerated `input_key`
            # CHECK was dropped (#770 finding 1, init.md rule 22) because it forced a
            # migration onto every new metric. This is the replacement guarantee --
            # unregistered evidence never lands in staging, it just does not require
            # a schema edit to keep it that way.
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


# The ticker of each listing of the given issuers in the run. The listing-identity cells name
# it, and the stored daily series are keyed by ticker.
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

# The stored company-facts bytes behind the issuer's financial-fact cells of the run.
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
  and p.normalized_payload ->> 'issuer_id' = %s
  and v.raw_fetch_id is not null
order by 1
"""

#: One captured cell of the bridge: (canonical listing, payload, confidence, the observation's knowable_at).
_Cell = tuple[str, dict, Decimal, datetime]


def _counts_on_close_basis(
    connection: psycopg.Connection[Any],
    run_id: str,
    financial: Mapping[str, _Cell],
    price: Mapping[str, _Cell],
    *,
    parser_version: str,
) -> dict[str, OnCloseBasis | Unavailable]:
    """Each issuer whose filed share count meets a captured close: the count on that close's basis.

    The close's session is the UTC date of its knowable time, the convention every captured price
    cell follows (``composition._is_settled_session``). The splits come from the two stored daily
    series of the issuer's listings in this run. A symbol whose series fail the derivation makes
    its issuer unavailable and leaves the other issuers alone. When no count meets a close, it sends
    no query.
    """
    meeting = sorted(
        issuer_id
        for issuer_id in financial.keys() & price.keys()
        if financial[issuer_id][1].get(SHARES_KEY) is not None and price[issuer_id][1].get("close") is not None
    )
    if not meeting:
        return {}
    tickers: dict[str, list[str]] = {}
    for issuer_id, ticker in connection.execute(_LISTING_TICKERS_SQL, (run_id, meeting)).fetchall():
        tickers.setdefault(str(issuer_id), []).append(str(ticker))
    symbols = sorted({ticker for names in tickers.values() for ticker in names})
    unadjusted = load_daily_series(connection, symbols, adjust=UNADJUSTED)
    split_adjusted = load_daily_series(connection, symbols, adjust=SPLIT_ADJUSTED)
    histories = {symbol: split_history(symbol, unadjusted, split_adjusted) for symbol in symbols}
    ruleset = cache(partial(resolve_ruleset, connection))
    outcomes: dict[str, OnCloseBasis | Unavailable] = {}
    for issuer_id in meeting:
        _listing, payload, _confidence, observed_at = financial[issuer_id]
        _price_listing, close_payload, _close_confidence, close_observed_at = price[issuer_id]
        names = sorted(tickers.get(issuer_id, ()))
        outcome = live_on_close_basis(
            LiveShareCount(
                shares=Decimal(str(payload[SHARES_KEY])),
                knowable_at=observed_at,
                entry=(payload.get("vintage") or {}).get(SHARES_KEY),
                close_session=close_observed_at.astimezone(UTC).date(),
                close_knowable_at=close_observed_at,
                close=Decimal(str(close_payload["close"])),
            ),
            [histories[symbol] for symbol in names],
            documents=partial(_company_facts_documents, connection, run_id, issuer_id, parser_version),
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
    connection: psycopg.Connection[Any], run_id: str, issuer_id: str, parser_version: str
) -> list[dict[str, Any]]:
    """The stored company-facts documents behind one issuer's financial-fact cells of the run.

    A document that cannot be read is left out and logged. The count then cannot be classed,
    and ``live_on_close_basis`` makes the issuer unavailable.
    """
    documents: list[dict[str, Any]] = []
    for (raw_fetch_id,) in connection.execute(_FINANCIAL_SOURCES_SQL, (run_id, parser_version, issuer_id)).fetchall():
        try:
            documents.append(json.loads(raw_store.get_payload(connection, raw_fetch_id)))
        except (LookupError, StorageError, ValueError) as error:
            log.warning("share basis: raw.fetches:%s of %s cannot be read: %s", raw_fetch_id, issuer_id, error)
    return documents


def _on_close_basis_rows(
    rows: Sequence[tuple[str, str, Decimal, datetime, str | None]], outcome: OnCloseBasis | Unavailable | None
) -> list[tuple[str, str, Decimal, datetime, str | None]]:
    """The financial input rows with the scalar share count on the share basis of the close.

    ``None`` means the count meets no close: the rows stay as they are. An ``Unavailable`` drops
    the share row. A factor of 1 keeps the row unchanged, so a run without a split seeds the
    same bytes as before.
    """
    if outcome is None or (isinstance(outcome, OnCloseBasis) and outcome.factor == 1):
        return list(rows)
    kept: list[tuple[str, str, Decimal, datetime, str | None]] = []
    for row in rows:
        input_key, _value, confidence, _knowable_at, fiscal_period = row
        if input_key != SHARES_KEY or fiscal_period is not None:
            kept.append(row)
        elif isinstance(outcome, OnCloseBasis):
            kept.append((input_key, str(outcome.value), confidence, outcome.knowable_at, fiscal_period))
    return kept


def run_strategy_replay_for_cutoff(
    connection: psycopg.Connection[Any],
    *,
    cutoff: datetime,
    executed_at: datetime,
    risk_free_rate: Decimal,
    capture_run_id: str,
) -> tuple[str, int, str]:
    """Evaluate the frozen strategy over the captured staging inputs for one cutoff and
    persist ``mart.strategy_runs``/``mart.strategy_decisions``. The caller supplies the risk-free
    rate. The tick passes the rate that its core materialization used (#1255), so one tick reads
    one rate. The rate is part of the run identity.

    ``capture_run_id`` is the capture this evaluation belongs to, and the run is bound to
    it (#877): a cutoff alone can name two captures since forced ticks (#874) exist, so
    every reader that pairs a decision with a core result, or a strategy run with the
    governed head, resolves through that binding. Required, so a caller cannot publish a
    strategy run that no capture owns."""
    definition = load_strategy_definition()
    gateway = StrategyBacktestGateway(connection)
    issuer_classes = _issuer_classes_for_run(connection, capture_run_id)
    issuer_inputs = [
        replace(item, issuer_class=issuer_classes.get(item.issuer_id)) for item in gateway.issuer_inputs(cutoff)
    ]
    evaluated = evaluate_cutoff(
        issuer_inputs,
        definition=definition,
        cutoff_at=cutoff,
        risk_free_rate=risk_free_rate,
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


def _issuer_classes_for_run(connection: psycopg.Connection[Any], run_id: str) -> dict[str, IssuerClass]:
    """The operating class of each issuer in one capture run (#1176). Read from the captured
    financial-fact observations, the same rows the strategy inputs are seeded from. The class
    selects the labor-efficiency metric (`LABOR_EFFICIENCY_BY_CLASS`). An issuer with no class
    here gets `missing_issuer_class` in the evaluator."""
    rows = connection.execute(
        """
        select distinct p.normalized_payload ->> 'issuer_id', p.normalized_payload ->> 'operating_branch'
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        where ob.run_id = %s
          and o.semantic_type = 'financial-fact'
        """,
        (run_id,),
    ).fetchall()
    classes: dict[str, IssuerClass] = {}
    for issuer_id, branch in rows:
        if branch is None:
            continue
        issuer_class = IssuerClass(branch)
        if classes.setdefault(issuer_id, issuer_class) != issuer_class:
            raise ValueError(f"{issuer_id}: two operating branches in capture run {run_id}")
    return classes


def persist_strategy_input_coverage(
    connection: psycopg.Connection[Any],
    run_id: str,
    *,
    cutoff: datetime,
) -> tuple[int, int]:
    """#496: the L2 funnel metric — one `mart.strategy_input_coverage` row per
    issuer of this run, measuring seeded staging inputs against the FROZEN
    definition's `required_input_keys()` (derived semantics, not a hardcoded
    list). Runs right after `seed_strategy_inputs_from_capture` in the same
    tick transaction; the same latest-vintage-per-(issuer, key) read the
    gateway uses, so the metric can never disagree with the replay's view.
    Returns (complete_issuers, total_issuers)."""
    required = sorted(load_strategy_definition().required_input_keys())
    issuers = [
        row[0]
        for row in connection.execute(
            "select distinct issuer_id from mart.topt_core_meta_info where run_id = %s order by 1",
            (run_id,),
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
