"""#528 acceptance 4, against a real materialized governed head: `node-sign-policy` (the
nightly suite's replacement for `gppe-not-negative`) asserts exactly the sign policy the
metric forest declares, shows every declared low signal, and is red on a synthetic violating
row. No exemption is involved anywhere in this file."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from factors.forest import FOREST, Forest, IssuerClass, SignPolicy
from truealpha_runtime.testing import load_tool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from production_topt.test_plausibility_gate import (  # noqa: E402
    _bypass_append_only,
    _materialized_run,
    _point_head_at,
    connection,  # noqa: F401  (the fixture)
)

suite = load_tool("output_invariants")


class _Borrowed:
    """The suite opens and closes its own connection; the seeded rows live in this test's
    uncommitted transaction, so lend it the test's connection and keep it open."""

    def __init__(self, conn: Any) -> None:
        self._connection = conn

    def __enter__(self) -> Any:
        return self._connection

    def __exit__(self, *_: object) -> None:
        return None


def _invariant(queries: tuple[str, str | None] | None = None):
    (shipped,) = [invariant for invariant in suite.INVARIANTS if invariant.id == "node-sign-policy"]
    if queries is None:
        return shipped
    violations, signals = queries
    return suite.Invariant(shipped.id, shipped.claim, violations, shipped.population, signals)


def _check(conn, invariant) -> int:
    return suite.check(
        "postgresql://borrowed",
        require_coverage=True,
        exemptions={},
        invariants=(invariant,),
        connect=lambda _url: _Borrowed(conn),
    )


def _set_jpm(conn, run_id: str, *, gppe: str, efficiency: str | None, adjusted: str | None) -> None:
    _bypass_append_only(conn)
    conn.execute(
        """
        update mart.topt_gppe_results
        set availability = 'available', gppe = %s, operating_efficiency = %s, capital_adjusted_gross_profit = %s
        where run_id = %s and listing_id = 'listing:xnys:jpm'
        """,
        (gppe, efficiency, adjusted, run_id),
    )


def _allow_grandfathered_shape(conn) -> None:
    """`topt_gppe_results_uniform_values_check` (0030) is NOT VALID: it refuses a NULL beside a
    value on new writes but grandfathers older rows, so the suite must still name that shape.
    Dropped inside this test's transaction only (rolled back by the fixture)."""
    conn.execute("alter table mart.topt_gppe_results drop constraint topt_gppe_results_uniform_values_check")


@pytest.fixture
def head(connection):  # noqa: F811
    run_id, snapshot = _materialized_run(connection)
    _point_head_at(connection, run_id, snapshot)
    return run_id


def test_the_governed_head_holds_and_shows_a_declared_low_signal(connection, head, capsys) -> None:  # noqa: F811
    assert _check(connection, _invariant()) == 0
    out = capsys.readouterr().out
    assert "ok       node-sign-policy" in out and "SIGNAL" not in out

    _set_jpm(connection, head, gppe="-514726.13", efficiency="-514726.13", adjusted="-163940000000")
    assert _check(connection, _invariant()) == 0
    signals = [line for line in capsys.readouterr().out.splitlines() if line.startswith("  SIGNAL")]
    assert len(signals) == 2, signals  # gppe and capital_adjusted_gross_profit, each by name
    assert all("listing:xnys:jpm" in line and "financial" in line and "sign-is-signal" in line for line in signals)
    assert any("-514726.13" in line for line in signals)


def test_a_sign_a_ratio_cannot_produce_is_red(connection, head, capsys) -> None:  # noqa: F811
    """gppe = capital_adjusted_gross_profit / employees_total with a non-negative headcount:
    the two signs agree, whatever the policy of either."""
    _set_jpm(connection, head, gppe="-514726.13", efficiency="-514726.13", adjusted="163940000000")
    assert _check(connection, _invariant()) == 1
    err = capsys.readouterr().err
    assert "listing:xnys:jpm" in err and "sign of capital_adjusted_gross_profit" in err
    # a published ratio whose numerator is absent is named too: sign(NULL) would compare as NULL
    _allow_grandfathered_shape(connection)
    _set_jpm(connection, head, gppe="-514726.13", efficiency="-514726.13", adjusted=None)
    assert _check(connection, _invariant()) == 1
    assert "sign of capital_adjusted_gross_profit" in capsys.readouterr().err


def test_two_columns_carrying_one_node_must_agree(connection, head, capsys) -> None:  # noqa: F811
    _set_jpm(connection, head, gppe="-514726.13", efficiency="514726.13", adjusted="-163940000000")
    assert _check(connection, _invariant()) == 1
    assert "same node as gppe" in capsys.readouterr().err
    # a NULL beside a value is a disagreement, not a pass
    _allow_grandfathered_shape(connection)
    _set_jpm(connection, head, gppe="-514726.13", efficiency=None, adjusted="-163940000000")
    assert _check(connection, _invariant()) == 1
    err = capsys.readouterr().err
    assert "same node as gppe" in err and "operating_efficiency" in err and " null " in err


def test_a_negative_value_the_node_forbids_is_red(connection, head, capsys, monkeypatch) -> None:  # noqa: F811
    """Red-proof of the policy itself: the same JPM row, under a forest whose GPPE node forbids
    a negative financial value, fails the suite — and prints no signal for it."""
    strict_nodes = tuple(
        node.model_copy(
            update={"sign_policy": {**node.sign_policy, IssuerClass.FINANCIAL: SignPolicy.MUST_BE_NON_NEGATIVE}}
        )
        if node.key == "gppe"
        else node
        for node in FOREST.nodes
    )
    monkeypatch.setattr(suite, "FOREST", Forest(nodes=strict_nodes, trees=FOREST.trees))
    strict = _invariant(suite._sign_policy_queries())
    _set_jpm(connection, head, gppe="-514726.13", efficiency="-514726.13", adjusted="-163940000000")
    assert _check(connection, strict) == 1
    captured = capsys.readouterr()
    assert "listing:xnys:jpm" in captured.err and "must-be-non-negative" in captured.err
    assert all("gppe:" not in line for line in captured.out.splitlines() if line.startswith("  SIGNAL"))
