"""#1518 criterion 3a: one tick gives one labor efficiency for each issuer.

The deployed op `run_topt_live_tick` runs offline over the fixture universe of `test_live_input_age`.
The capture, the snapshot, the core materialization, the strategy seed, the replay and the node-value
writer are real. The bank of the fixture (JPM) has a tangible common equity of 40,000,000, a fifth of
its total assets (200,000,000). So a core that charges total assets and a decision that charges
tangible common equity give two numbers. The tick must give one.

Hand values. The rate is the one that the run recorded. The numbers come from the seeded bundles:

    bank       = (80,000,000 - 40,000,000 x rate) / 164,000     (TCE = 50M - 5M - (3M + 2M) = 40M)
    other      = (210,000,000 - 200,000,000 x rate) / 164,000

The values are rounded to 2 places, half-even, as the definition quantizes the decision.
"""

from __future__ import annotations

import sys
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import psycopg
from data_engine.datahub.market_prices import DEFAULT_TOPT_SYMBOLS
from truealpha_contracts.strategy import load_live_strategy_definition

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_live_input_age import AGE_DAYS, Tick  # noqa: E402
from test_live_input_age import tick as _tick_fixture  # noqa: E402

#: The module-scoped fixture of the deployed tick, registered in this module under its own name.
tick = _tick_fixture

_BANK = "JPM"
_HEADCOUNT = Decimal("164000")
#: One issuer of the fixture that is not a bank and not aged past the limit.
_OTHER = next(ticker for ticker in DEFAULT_TOPT_SYMBOLS if ticker not in AGE_DAYS and ticker != _BANK)


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), ROUND_HALF_EVEN)


def _issuer_id(reader: psycopg.Connection, tick: Tick, ticker: str) -> str:
    return reader.execute(
        """
        select distinct p.normalized_payload ->> 'issuer_id'
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_observation_payloads p on p.observation_id = oo.observation_id
        where ob.run_id = %s and p.normalized_payload ->> 'ticker' = %s
        """,
        (tick.metadata["capture_run_id"], ticker),
    ).fetchone()[0]


def _served(tick: Tick, ticker: str) -> tuple[Decimal, Decimal, str, str]:
    """The core value, the decision value, the decision's metric name and the strategy version of one issuer."""
    with psycopg.connect(tick.url) as reader:
        issuer_id = _issuer_id(reader, tick, ticker)
        core = reader.execute(
            "select operating_efficiency, gppe, availability from mart.topt_core_results where run_id = %s and issuer_id = %s",
            (tick.metadata["capture_run_id"], issuer_id),
        ).fetchone()
        decision = reader.execute(
            """
            select d.capital_adjusted_labor_efficiency, d.labor_efficiency_metric, r.strategy_version
            from mart.strategy_decisions d join mart.strategy_runs r using (strategy_run_id)
            where d.strategy_run_id = %s and d.issuer_id = %s
            """,
            (tick.metadata["strategy_run_id"], issuer_id),
        ).fetchone()
    assert core is not None and core[2] == "available", (ticker, core)
    assert decision is not None and decision[0] is not None, (ticker, decision)
    assert core[0] == core[1], "the published efficiency is the gppe"
    return core[0], decision[0], decision[1], decision[2]


def _rate(tick: Tick) -> Decimal:
    with psycopg.connect(tick.url) as reader:
        (rate,) = reader.execute(
            "select risk_free_value from mart.strategy_runs where strategy_run_id = %s",
            (tick.metadata["strategy_run_id"],),
        ).fetchone()
    return rate


def test_the_core_and_the_decision_of_a_bank_equal_the_hand_derived_tce_value(tick: Tick) -> None:
    rate = _rate(tick)
    core, decision, metric, _version = _served(tick, _BANK)
    tce_value = _round((Decimal("80000000") - Decimal("40000000") * rate) / _HEADCOUNT)
    uniform_value = _round((Decimal("80000000") - Decimal("200000000") * rate) / _HEADCOUNT)

    assert tce_value != uniform_value, "the fixture must separate the two capital bases"
    assert _round(core) == tce_value, f"the core charged another base than the definition pins: {core}"
    assert decision == tce_value
    assert metric == "gppe_banking_tce_v1"


def test_the_core_and_the_decision_of_another_class_equal_the_hand_derived_uniform_value(tick: Tick) -> None:
    rate = _rate(tick)
    core, decision, metric, _version = _served(tick, _OTHER)
    uniform_value = _round((Decimal("210000000") - Decimal("200000000") * rate) / _HEADCOUNT)

    assert _round(core) == uniform_value
    assert decision == uniform_value
    assert metric == "gppe_uniform_charge_v0"


def test_the_run_names_definition_1_1_0_and_the_root_node_holds_the_pinned_tree_hash(tick: Tick) -> None:
    live = load_live_strategy_definition()
    assert live.labor_efficiency_tree_keys is not None
    pin = live.labor_efficiency_tree_keys["financial"]
    _core, _decision, _metric, version = _served(tick, _BANK)
    with psycopg.connect(tick.url) as reader:
        run_hash = reader.execute(
            "select definition_content_sha256 from mart.strategy_runs where strategy_run_id = %s",
            (tick.metadata["strategy_run_id"],),
        ).fetchone()[0]
        root = reader.execute(
            """
            select tree_version, tree_sha256 from mart.metric_node_values
            where run_id = %s and issuer_id = %s and node_key = 'gppe_banking_tce_v1'
            """,
            (tick.metadata["capture_run_id"], _issuer_id(reader, tick, _BANK)),
        ).fetchone()
    assert version == "1.1.0"
    assert run_hash == live.content_sha256
    assert root == (pin.tree_version, pin.tree_sha256)


def test_the_frozen_snapshot_carries_the_banking_leaves_of_the_bank_only_and_an_old_snapshot_reloads(
    tick: Tick,
) -> None:
    """The member of the bank holds its four banking leaves. Every other member holds none, so its content
    hash does not move. A snapshot stored before #1518 has no leaves: it reloads, and its bank is
    unavailable with the four fields named (a replay of such a run reads nothing else)."""
    from data_engine.datahub.production_topt.materialization import ToptCoreSnapshot
    from factors.production_topt import (
        TCE_SNAPSHOT_FIELDS,
        GppeV0Definition,
        ToptCoreAvailability,
        compute_topt_gppe,
    )

    with psycopg.connect(tick.url) as reader:
        stored = reader.execute(
            "select payload from staging.topt_core_snapshots where run_id = %s", (tick.metadata["capture_run_id"],)
        ).fetchone()
    assert stored is not None
    payload = dict(stored[0])
    banks = [member for member in payload["members"] if member["operating_branch"] == "financial"]
    others = [member for member in payload["members"] if member["operating_branch"] != "financial"]
    assert len(banks) == 1 and others
    assert set(TCE_SNAPSHOT_FIELDS) <= set(banks[0]), "the bank carries its four leaves"
    assert not any(set(TCE_SNAPSHOT_FIELDS) & set(member) for member in others), "no other member carries one"

    for member in payload["members"]:
        for name in TCE_SNAPSHOT_FIELDS:
            member.pop(name, None)
    reloaded = ToptCoreSnapshot.model_validate(payload)
    (bank_input,) = [item for item in reloaded.factor_inputs() if item.operating_branch.value == "financial"]
    result = compute_topt_gppe(
        bank_input,
        invocation_id=f"topt-gppe-invocation:{'f' * 64}",
        gppe_definition=GppeV0Definition(risk_free_rate=_rate(tick)),
        definition=load_live_strategy_definition(),
    )

    assert result.availability is ToptCoreAvailability.UNAVAILABLE
    assert {reason.value for reason in result.reason_codes} == {f"missing_{name}" for name in TCE_SNAPSHOT_FIELDS}
