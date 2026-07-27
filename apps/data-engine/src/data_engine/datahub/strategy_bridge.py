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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from factors.composite.strategy_evaluator import evaluate_cutoff
from truealpha_contracts.strategy import LargeModelValueV0Definition

from data_engine.core_strategy_replay import _load_corpus, _to_decision
from data_engine.datahub.production_topt.parser_identity import PARSER_VERSION as PRIMARY_PARSER_VERSION
from data_engine.strategy_backtest_gateway import StrategyBacktestGateway
from data_engine.strategy_replay_repository import write_replay

_STRATEGY_FINANCIAL_KEYS = ("gross_profit", "total_assets", "headcount", "revenue", "shares_outstanding")


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
        select o.semantic_type, o.confidence, p.normalized_payload
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

    financial: dict[str, tuple[str, dict, Decimal]] = {}  # issuer -> (listing, payload, confidence)
    price: dict[str, tuple[str, dict, Decimal]] = {}
    for semantic_type, confidence, payload in rows:
        issuer_id, listing_id = payload["issuer_id"], payload["listing_id"]
        bucket = financial if semantic_type == "financial-fact" else price
        current = bucket.get(issuer_id)
        if current is None or listing_id < current[0]:  # canonical listing = lowest listing_id
            bucket[issuer_id] = (listing_id, payload, Decimal(str(confidence)))

    knowable_at = cutoff - timedelta(minutes=58)
    written = 0
    for issuer_id in sorted(set(financial) | set(price)):
        inputs: list[tuple[str, str, Decimal]] = []
        if issuer_id in financial:
            _listing, payload, confidence = financial[issuer_id]
            for key in _STRATEGY_FINANCIAL_KEYS:
                value = payload.get(key)
                if value is not None:
                    inputs.append((key, value, confidence))
        if issuer_id in price:
            _listing, payload, confidence = price[issuer_id]
            close = payload.get("close")
            if close is not None:
                inputs.append(("last_close", close, confidence))
        for input_key, value, confidence in inputs:
            connection.execute(
                """
                insert into staging.strategy_backtest_inputs
                    (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
                values (%s, %s, %s, %s, %s, %s)
                """,
                (issuer_id, cutoff, input_key, value, confidence, knowable_at),
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
