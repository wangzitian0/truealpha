"""The capture → strategy bridge (#429).

Two steps the deployed tick runs after a capture materializes:

* ``seed_strategy_inputs_from_capture`` lands the captured cells' provenance-neutral
  factor inputs into ``staging.strategy_backtest_inputs``, replacing the golden-fixture
  seeding that previously fed the deployed canary.
* ``run_strategy_replay_for_cutoff`` drives the single-source ``strategy_evaluator`` over
  the ``StrategyBacktestGateway`` for that cutoff and persists ``mart.strategy_runs`` /
  ``mart.strategy_decisions``.

The only fixture-derived object read here is the frozen strategy DEFINITION (#21) —
versioned strategy configuration, not input data. Golden inputs, decisions and rates are
never read (#429 invariant I2).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from factors.composite.strategy_evaluator import evaluate_cutoff
from truealpha_contracts.strategy import LargeModelValueV0Definition

from data_engine.core_strategy_replay import _load_corpus, _to_decision
from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION as PRIMARY_PARSER_VERSION
from data_engine.strategy_backtest_gateway import StrategyBacktestGateway
from data_engine.strategy_replay_repository import write_replay

# `net_income` and `earnings_cagr_3y` are module 1's two additions (#284). The CAGR is a
# rate the adapter reduced from the annual series rather than a raw fact, and it crosses
# here as an ordinary input because `strategy_backtest_inputs` has no fiscal-period
# dimension — giving it one means the read path #530 tracks.
_STRATEGY_FINANCIAL_KEYS = (
    "gross_profit",
    "total_assets",
    "headcount",
    "revenue",
    "shares_outstanding",
    "net_income",
)

# Keys whose value describes a fiscal PERIOD rather than an instant, so one issuer lands
# several rows per cutoff — one per period — distinguished by `fiscal_period` (0043).
# `earnings_cagr_3y` used to be here as a scalar; the series it was reduced from now
# travels instead and `factors.base.peg` does the reducing (init.md rule 2).
_STRATEGY_PERIODIC_KEYS = {"net_income": "net_income_by_period"}


def load_strategy_definition() -> LargeModelValueV0Definition:
    """The frozen large_model_value_v0 strategy definition (#21). Versioned strategy
    CONFIGURATION (formula constants and bands) — the deployed job never reads the
    corpus's golden inputs, decisions, or rates (#429 invariant I2)."""
    corpus = _load_corpus()
    return LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))


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
    the integration-corpus vintage through the same code.
    """
    rows = connection.execute(
        """
        select o.semantic_type, o.confidence, p.normalized_payload, o.knowable_at
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        where ob.run_id = %s
          and o.parser_version = %s
          and o.semantic_type in ('financial-fact', 'market-price')
        """,
        (run_id, parser_version),
    ).fetchall()

    # issuer -> (canonical listing, payload, confidence, the observation's own knowable_at)
    financial: dict[str, tuple[str, dict, Decimal, datetime]] = {}
    price: dict[str, tuple[str, dict, Decimal, datetime]] = {}
    for semantic_type, confidence, payload, observed_at in rows:
        issuer_id, listing_id = payload["issuer_id"], payload["listing_id"]
        bucket = financial if semantic_type == "financial-fact" else price
        current = bucket.get(issuer_id)
        if current is None or listing_id < current[0]:  # canonical listing = lowest listing_id
            bucket[issuer_id] = (listing_id, payload, Decimal(str(confidence)), observed_at)

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
            for key in _STRATEGY_FINANCIAL_KEYS:
                value = payload.get(key)
                if value is not None:
                    inputs.append((key, value, confidence, observed_at, None))
            for key, payload_key in _STRATEGY_PERIODIC_KEYS.items():
                # The period tag mirrors staging's own encoding so `factors.base.peg`
                # parses one shape wherever the series came from. Only the end date is
                # known here, and an annual period's start is its end less a year; the
                # factor re-checks the duration floor rather than trusting the tag.
                for period_end, value in sorted((payload.get(payload_key) or {}).items()):
                    end = date.fromisoformat(period_end)
                    start = end.replace(year=end.year - 1) + timedelta(days=1)
                    tag = f"FY{end.year}:FY:{start.isoformat()}:{end.isoformat()}"
                    inputs.append((key, value, confidence, observed_at, tag))
        if issuer_id in price:
            _listing, payload, confidence, observed_at = price[issuer_id]
            close = payload.get("close")
            if close is not None:
                inputs.append(("last_close", close, confidence, observed_at, None))
        for input_key, value, confidence, knowable_at, fiscal_period in inputs:
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


def run_strategy_replay_for_cutoff(
    connection: psycopg.Connection[Any],
    *,
    cutoff: datetime,
    executed_at: datetime,
    risk_free_rate: Decimal,
) -> tuple[str, int, str]:
    """Evaluate the frozen strategy over the captured staging inputs for one cutoff and
    persist ``mart.strategy_runs``/``mart.strategy_decisions``. The risk-free rate is the
    same 0.05 the GPPE materialization pins, supplied explicitly by the caller."""
    definition = load_strategy_definition()
    gateway = StrategyBacktestGateway(connection)
    issuer_inputs = gateway.issuer_inputs(cutoff)
    evaluated = evaluate_cutoff(issuer_inputs, definition=definition, cutoff_at=cutoff, risk_free_rate=risk_free_rate)
    cutoff_key = cutoff.astimezone(UTC).isoformat()
    decisions = sorted(
        (_to_decision(item, cutoff_key) for item in evaluated),
        key=lambda item: (item.cutoff_at, item.issuer_id),
    )
    snapshot_id = gateway.snapshot_id(cutoff)
    run_id, decision_ids = write_replay(
        connection, decisions, definition, executed_at=executed_at, snapshot_id=snapshot_id
    )
    return run_id, len(decision_ids), snapshot_id


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
