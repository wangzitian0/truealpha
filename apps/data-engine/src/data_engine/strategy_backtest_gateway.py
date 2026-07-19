"""#395: run the base large_model_value_v0 strategy on captured data through a real
data boundary, not the checked-in golden JSON.

`seed_strategy_backtest_inputs` lands the strategy's provenance-neutral factor inputs
(grounded in the checked-in samples the #21 golden is built from) into
`staging.strategy_backtest_inputs`. `StrategyBacktestGateway` reads them back per
cutoff into the `strategy_evaluator` input shape and derives a content-addressed
PIT `snapshot_id`. `run_backtest_from_staging` then drives the single-source
evaluator over that gateway and maps the decisions onto the mart `Decision`
dataclass -- so the replay consumes staging, not the fixture, and each run binds the
exact snapshot it was computed from.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from factors.composite.strategy_evaluator import IssuerInput, evaluate_cutoff
from psycopg import Connection
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.strategy import LargeModelValueV0Definition

from data_engine.core_strategy_replay import Decision, _risk_free_rate, _to_decision


def seed_strategy_backtest_inputs(connection: Connection[Any], corpus: dict[str, Any]) -> int:
    """Land each golden decision's provenance-neutral inputs into
    staging.strategy_backtest_inputs. Returns the number of input rows written."""

    written = 0
    for decision in corpus["golden_decision_set"]["decisions"]:
        issuer_id = decision["issuer"]["id"]
        cutoff_at = decision["cutoff_at"]
        for record in decision["inputs"]:
            connection.execute(
                """
                insert into staging.strategy_backtest_inputs
                    (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
                values (%s, %s, %s, %s, %s, %s)
                """,
                (
                    issuer_id,
                    cutoff_at,
                    record["input_key"],
                    record["value"],
                    record["confidence"],
                    record["knowable_at"],
                ),
            )
            written += 1
    return written


class StrategyBacktestGateway:
    """The strategy's data boundary: it reads captured factor inputs from
    staging.strategy_backtest_inputs and never sees the fixture or any provenance."""

    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    def _rows_for_cutoff(self, cutoff_at: str) -> list[tuple[str, str, Any, Any]]:
        # Latest vintage per (issuer, input_key) at the cutoff -- a restatement lands a
        # new row and supersedes by recorded_at, never overwriting the prior one.
        return self._connection.execute(
            """
            select distinct on (issuer_id, input_key) issuer_id, input_key, value, confidence
            from staging.strategy_backtest_inputs
            where cutoff_at = %s
            order by issuer_id, input_key, recorded_at desc
            """,
            (cutoff_at,),
        ).fetchall()

    def issuer_inputs(self, cutoff_at: str) -> list[IssuerInput]:
        by_issuer: dict[str, dict[str, tuple[Decimal, Decimal]]] = {}
        for issuer_id, input_key, value, confidence in self._rows_for_cutoff(cutoff_at):
            by_issuer.setdefault(issuer_id, {})[input_key] = (Decimal(str(value)), Decimal(str(confidence)))
        return [IssuerInput(issuer_id=issuer_id, records=records) for issuer_id, records in sorted(by_issuer.items())]

    def snapshot_id(self, cutoff_at: str) -> str:
        payload = sorted(
            (issuer_id, input_key, str(value), str(confidence))
            for issuer_id, input_key, value, confidence in self._rows_for_cutoff(cutoff_at)
        )
        return f"strategy-snapshot:{canonical_sha256(payload)}"

    def run_snapshot_id(self, cutoff_ats: list[str]) -> str:
        """One PIT identity for a multi-cutoff run: the content hash of the per-cutoff
        snapshot ids."""
        return f"strategy-snapshot:{canonical_sha256([self.snapshot_id(cutoff) for cutoff in sorted(cutoff_ats)])}"


def run_backtest_from_staging(
    connection: Connection[Any], corpus: dict[str, Any], definition: LargeModelValueV0Definition
) -> tuple[list[Decision], str]:
    """Evaluate the strategy for every cutoff over the captured staging inputs and map
    the decisions onto the mart Decision dataclass. Returns the decisions plus the
    run-level snapshot id binding the exact captured inputs."""

    gateway = StrategyBacktestGateway(connection)
    rates = corpus["golden_decision_set"]["risk_free_rates"]
    cutoff_ats = sorted({decision["cutoff_at"] for decision in corpus["golden_decision_set"]["decisions"]})

    decisions: list[Decision] = []
    for cutoff_at in cutoff_ats:
        as_of = datetime.fromisoformat(cutoff_at.replace("Z", "+00:00"))
        risk_free_rate = _risk_free_rate(rates, cutoff_at)
        evaluated = evaluate_cutoff(
            gateway.issuer_inputs(cutoff_at), definition=definition, cutoff_at=as_of, risk_free_rate=risk_free_rate
        )
        decisions.extend(_to_decision(item, cutoff_at) for item in evaluated)

    decisions.sort(key=lambda item: (item.cutoff_at, item.issuer_id))
    return decisions, gateway.run_snapshot_id(cutoff_ats)
