"""The gateway hands the evaluator the instant each winning input became knowable (#1255).

These tests need no database. `rows_at` ends each row with the `knowable_at` of its winning vintage.
`issuer_inputs_from_rows` binds that instant to the value it came with. `snapshot_id_from_rows` leaves
the instant out of the content identity, so every stored snapshot id keeps its bytes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from data_engine.strategy_backtest_gateway import InputRow, issuer_inputs_from_rows, snapshot_id_from_rows
from factors.composite.strategy_evaluator import evaluate_cutoff
from factors.forest import IssuerClass
from truealpha_contracts.strategy import ExclusionReason, load_live_strategy_definition

CUTOFF = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
FRESH = timedelta(days=3)

#: The id that the code before #1255 gave these rows (five-field rows, computed on main 91445c2).
#: A change to the payload of `snapshot_id_from_rows` changes this value and every stored run identity.
PINNED_SNAPSHOT_ID = "strategy-snapshot:4da6212db480e359dbe2a3c3c20c7b6bd5a3899d3a8966f5b39bb233b8f9a94f"
PINNED_EMPTY_SNAPSHOT_ID = "strategy-snapshot:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


def _at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def _pinned_rows(knowable_at: datetime) -> list[InputRow]:
    """Seven rows: scalar and periodic, an int, a Decimal and a Decimal with trailing zeros."""
    return [
        ("issuer:alpha", "gross_profit", Decimal("5033000000"), Decimal("0.90"), None, knowable_at),
        ("issuer:alpha", "last_close", Decimal("101.5"), Decimal("0.85"), None, knowable_at),
        ("issuer:alpha", "headcount", 42000, Decimal("0.70"), None, knowable_at),
        ("issuer:alpha", "net_income", Decimal("100"), Decimal("0.90"), "FY2022:FY:2022-01-01:2022-12-31", knowable_at),
        ("issuer:alpha", "net_income", Decimal("125"), Decimal("0.90"), "FY2023:FY:2023-01-01:2023-12-31", knowable_at),
        ("issuer:beta", "revenue", Decimal("20000000000"), Decimal("0.90"), None, knowable_at),
        ("issuer:beta", "shares_outstanding", Decimal("1000000000.000000"), Decimal("0.90"), None, knowable_at),
    ]


def test_the_snapshot_id_keeps_the_bytes_it_had_before_rows_carried_an_instant() -> None:
    """Red when `knowable_at` enters the payload: every run id and every stored snapshot id then changes."""
    assert snapshot_id_from_rows(_pinned_rows(_at(2026, 2, 1))) == PINNED_SNAPSHOT_ID
    assert snapshot_id_from_rows(list(reversed(_pinned_rows(_at(2026, 2, 1))))) == PINNED_SNAPSHOT_ID
    assert snapshot_id_from_rows([]) == PINNED_EMPTY_SNAPSHOT_ID


def test_the_snapshot_id_does_not_depend_on_the_instant_of_any_row() -> None:
    first = _pinned_rows(_at(2026, 2, 1))
    moved = _pinned_rows(_at(2024, 1, 1))
    moved[0] = (*moved[0][:5], _at(2020, 5, 5))

    assert snapshot_id_from_rows(first) == snapshot_id_from_rows(moved)


def test_the_snapshot_id_still_follows_the_value_the_confidence_and_the_period() -> None:
    """The control of the pin: the id is not a constant."""
    base = _pinned_rows(_at(2026, 2, 1))
    other_value = [(*base[0][:2], Decimal("5033000001"), *base[0][3:]), *base[1:]]
    other_confidence = [(*base[0][:3], Decimal("0.91"), *base[0][4:]), *base[1:]]
    other_period = [*base[:3], (*base[3][:4], "FY2021:FY:2021-01-01:2021-12-31", base[3][5]), *base[4:]]

    ids = {snapshot_id_from_rows(rows) for rows in (base, other_value, other_confidence, other_period)}

    assert len(ids) == 4


def test_a_scalar_row_gives_its_own_instant_to_its_key() -> None:
    [issuer] = issuer_inputs_from_rows(
        [
            ("issuer:a", "gross_profit", 10, "0.9", None, _at(2026, 2, 1)),
            ("issuer:a", "last_close", 5, "0.9", None, _at(2026, 6, 29)),
        ]
    )

    assert issuer.knowable_at == {"gross_profit": _at(2026, 2, 1), "last_close": _at(2026, 6, 29)}
    assert set(issuer.knowable_at) == set(issuer.records), "every value has an instant"


def test_a_periodic_key_takes_the_instant_of_the_latest_period_not_the_latest_instant() -> None:
    """FY2021 was restated in a filing that is newer than the FY2023 filing. The scalar answers with
    FY2023, so the instant of FY2023 is the instant of the key. Red when the key takes the maximum
    (or the minimum) instant over the series."""
    [issuer] = issuer_inputs_from_rows(
        [
            ("issuer:a", "net_income", 100, "0.9", "FY2021", _at(2025, 6, 1)),
            ("issuer:a", "net_income", 200, "0.9", "FY2022", _at(2023, 2, 1)),
            ("issuer:a", "net_income", 300, "0.9", "FY2023", _at(2024, 2, 1)),
        ]
    )

    assert issuer.records["net_income"][0] == Decimal(300)
    assert issuer.knowable_at["net_income"] == _at(2024, 2, 1)
    assert set(issuer.periodic_records["net_income"]) == {"FY2021", "FY2022", "FY2023"}


def test_the_instant_always_belongs_to_the_value_that_answers_for_the_key() -> None:
    """A key with a scalar row and a period series holds one answer. Its value and its instant come from
    the same row, whichever row the reader lets win."""
    answers = {Decimal(100): _at(2022, 2, 1), Decimal(300): _at(2024, 2, 1), Decimal(999): _at(2026, 3, 1)}
    for rows in (
        [
            ("issuer:a", "net_income", 100, "0.9", "FY2021", answers[Decimal(100)]),
            ("issuer:a", "net_income", 300, "0.9", "FY2023", answers[Decimal(300)]),
            ("issuer:a", "net_income", 999, "0.9", None, answers[Decimal(999)]),
        ],
        [
            ("issuer:a", "net_income", 999, "0.9", None, answers[Decimal(999)]),
            ("issuer:a", "net_income", 100, "0.9", "FY2021", answers[Decimal(100)]),
            ("issuer:a", "net_income", 300, "0.9", "FY2023", answers[Decimal(300)]),
        ],
    ):
        [issuer] = issuer_inputs_from_rows(rows)

        assert issuer.knowable_at["net_income"] == answers[issuer.records["net_income"][0]]


def test_an_issuer_with_one_periodic_key_and_nothing_else_still_gets_an_instant() -> None:
    [issuer] = issuer_inputs_from_rows([("issuer:a", "net_income", 100, "0.9", "FY2021", _at(2022, 2, 1))])

    assert issuer.knowable_at == {"net_income": _at(2022, 2, 1)}


def _complete_rows(issuer_id: str, ages: dict[str, timedelta]) -> list[InputRow]:
    values = {
        "gross_profit": "1000",
        "total_assets": "0",
        "headcount": "1",
        "revenue": "2000",
        "shares_outstanding": "5000",
        "last_close": "1",
    }
    return [
        (issuer_id, key, Decimal(value), Decimal("0.90"), None, CUTOFF - ages.get(key, FRESH))
        for key, value in values.items()
    ]


def test_rows_with_a_stale_required_input_exclude_the_issuer_and_old_periods_do_not() -> None:
    """The producer and the evaluator together, with no database.

    `issuer:old` holds gross profit 456 days old. `issuer:series` holds a net income series from 2008 on.
    Its early periods are over 6,000 days old. Both issuers hold fresh values otherwise.
    """
    series = [
        (
            "issuer:series",
            "net_income",
            Decimal(1000 + year),
            Decimal("0.90"),
            f"FY{year}:FY:{year}-01-01:{year}-12-31",
            _at(year + 1, 3, 1),
        )
        for year in range(2008, 2026)
    ]
    rows = [
        *_complete_rows("issuer:old", {"gross_profit": timedelta(days=456)}),
        *_complete_rows("issuer:series", {}),
        *series,
        *_complete_rows("issuer:edge", {"gross_profit": timedelta(days=455)}),
    ]

    decisions = {
        item.issuer_id: item
        for item in evaluate_cutoff(
            [replace(item, issuer_class=IssuerClass.NON_FINANCIAL) for item in issuer_inputs_from_rows(rows)],
            definition=load_live_strategy_definition(),
            cutoff_at=CUTOFF,
            risk_free_rate=Decimal("0"),
        )
    }

    assert decisions["issuer:old"].exclusion_reason is ExclusionReason.STALE_REQUIRED_INPUT
    assert decisions["issuer:series"].exclusion_reason is None, "a series of old periods is not a stale input"
    assert decisions["issuer:edge"].exclusion_reason is None, "an input of exactly 455 days is not stale"
