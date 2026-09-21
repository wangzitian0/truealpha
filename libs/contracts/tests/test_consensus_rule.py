"""The owner's confidence rule of 2026-09-17, one table (`grade_consensus`).

"三个源且误差小于千分之一，取中间的数，并且标记为置信度 100% high，可以忽略偶发的第四个源。
如果有两个源一致标记为 middle。其他情况是 low。"
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from truealpha_contracts.reconciliation import (
    CONSENSUS_RELATIVE_TOLERANCE,
    HIGH_CONSENSUS_CONFIDENCE,
    ConflictBehavior,
    ConsensusBand,
    ConsensusVote,
    ReconciliationCell,
    ReconciliationOutcome,
    ReconciliationPolicy,
    SourceAssertion,
    consensus_confidence,
    grade_consensus,
    reconcile_source_assertions,
)
from truealpha_contracts.universe import SubjectKind, SubjectRef

HIGH, MEDIUM, LOW, MISSING = ConsensusBand.HIGH, ConsensusBand.MEDIUM, ConsensusBand.LOW, ConsensusBand.MISSING


@dataclass(frozen=True)
class Case:
    name: str
    #: (group, value) in priority order: the first is the highest-priority group.
    votes: tuple[tuple[str, str], ...]
    band: ConsensusBand
    served: str | None
    agreeing: tuple[str, ...]
    outliers: tuple[str, ...] = ()


def _value(raw: str) -> Decimal | str:
    return raw.removeprefix("cat:") if raw.startswith("cat:") else Decimal(raw)


CASES = (
    Case(
        "3 of 3 agree -> high, median",
        (("a", "100.00"), ("b", "100.05"), ("c", "99.98")),
        HIGH,
        "100.00",
        ("a", "b", "c"),
    ),
    Case(
        "3 agree, median is the middle value not the priority value",
        (("a", "100.08"), ("b", "100.00"), ("c", "100.01")),
        HIGH,
        "100.01",
        ("a", "b", "c"),
    ),
    Case(
        "3 of 4 agree, one outlier -> high, outlier recorded",
        (("a", "342.72"), ("b", "342.72000"), ("c", "342.365"), ("d", "342.70")),
        HIGH,
        "342.72",
        ("a", "b", "d"),
        ("c",),
    ),
    Case(
        "the outlier may be the priority source",
        (("a", "110"), ("b", "100"), ("c", "100.05"), ("d", "99.99")),
        HIGH,
        "100.00",
        ("b", "c", "d"),
        ("a",),
    ),
    Case(
        "4 of 4 agree -> high, median is the mean of the middle two",
        (("a", "100.00"), ("b", "100.02"), ("c", "100.04"), ("d", "100.06")),
        HIGH,
        "100.030",
        ("a", "b", "c", "d"),
    ),
    Case(
        "2 of 4 against 2 -> medium, the higher-priority pair is served",
        (("a", "100"), ("b", "200"), ("c", "100.01"), ("d", "200.01")),
        MEDIUM,
        "100.005",
        ("a", "c"),
        ("b", "d"),
    ),
    Case("2 of 2 agree -> medium, mean", (("a", "567.75"), ("b", "567.80")), MEDIUM, "567.775", ("a", "b")),
    Case(
        "2 of 3 agree, third disagrees -> medium",
        (("a", "567.75"), ("b", "570.00"), ("c", "567.80")),
        MEDIUM,
        "567.775",
        ("a", "c"),
        ("b",),
    ),
    Case("2 disagree -> low, priority value", (("a", "100"), ("b", "101")), LOW, "100", ("a",), ("b",)),
    Case(
        "3 pairwise disagree -> low, priority value",
        (("a", "100"), ("b", "101"), ("c", "102")),
        LOW,
        "100",
        ("a",),
        ("b", "c"),
    ),
    Case("1 source -> low", (("a", "42"),), LOW, "42", ("a",)),
    Case("no source -> missing", (), MISSING, None, ()),
    # 0.1% boundary: |v - m| <= 0.001 * |m|, m the set's median.
    # A pair agrees when each lies within 0.1% of their mean, so up to ~0.2% apart.
    Case(
        "pair just inside the boundary agrees", (("a", "1000"), ("b", "1002.002002")), MEDIUM, "1001.001001", ("a", "b")
    ),
    Case("pair just past the boundary disagrees", (("a", "1000"), ("b", "1002.003")), LOW, "1000", ("a",), ("b",)),
    Case(
        "triple exactly at the boundary is high",
        (("a", "999"), ("b", "1000"), ("c", "1001")),
        HIGH,
        "1000",
        ("a", "b", "c"),
    ),
    Case(
        "triple past the boundary falls to the agreeing pair",
        (("a", "998.99"), ("b", "1000"), ("c", "1001")),
        MEDIUM,
        "1000.5",
        ("b", "c"),
        ("a",),
    ),
    Case(
        "0.3% apart no longer agrees (the superseded price tolerance)",
        (("a", "316.85"), ("b", "317.80")),
        LOW,
        "316.85",
        ("a",),
        ("b",),
    ),
    Case(
        "an exactly agreeing pair is served over a merely tolerable one",
        (("a", "567.75"), ("b", "568.42999"), ("c", "567.75")),
        MEDIUM,
        "567.75",
        ("a", "c"),
        ("b",),
    ),
    # A median of zero demands exact equality.
    Case("median 0, all zero -> high", (("a", "0"), ("b", "0.00"), ("c", "0")), HIGH, "0", ("a", "b", "c")),
    Case(
        "median 0, one non-zero -> the zero pair",
        (("a", "0"), ("b", "0"), ("c", "0.0000001")),
        MEDIUM,
        "0",
        ("a", "b"),
        ("c",),
    ),
    Case(
        "median 0 of a pair around zero never agrees", (("a", "-0.001"), ("b", "0.001")), LOW, "-0.001", ("a",), ("b",)
    ),
    Case(
        "negative values agree by magnitude",
        (("a", "-500"), ("b", "-500.4"), ("c", "-499.8")),
        HIGH,
        "-500",
        ("a", "b", "c"),
    ),
    # Categorical values agree only when equal, and never with a number.
    Case(
        "categorical all equal -> high",
        (("a", "cat:member"), ("b", "cat:member"), ("c", "cat:member")),
        HIGH,
        "cat:member",
        ("a", "b", "c"),
    ),
    Case(
        "categorical pair equal -> medium", (("a", "cat:member"), ("b", "cat:member")), MEDIUM, "cat:member", ("a", "b")
    ),
    Case("categorical differ -> low", (("a", "cat:NYSE"), ("b", "cat:NASDAQ")), LOW, "cat:NYSE", ("a",), ("b",)),
    Case("a category never agrees with a number", (("a", "cat:1"), ("b", "1")), LOW, "cat:1", ("a",), ("b",)),
)


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_the_owner_rule_table(case: Case) -> None:
    votes = [
        ConsensusVote(group_id=group, rank=rank, value=_value(raw)) for rank, (group, raw) in enumerate(case.votes)
    ]
    grade = grade_consensus(votes)
    assert grade.band is case.band
    expected = None if case.served is None else _value(case.served)
    assert grade.served_value == expected
    if isinstance(expected, Decimal):
        assert isinstance(grade.served_value, Decimal)
    assert grade.agreeing_group_ids == case.agreeing
    assert grade.outlier_group_ids == case.outliers
    # Arrival order never changes the grade.
    assert grade_consensus(list(reversed(votes))) == grade


def test_the_served_group_is_the_highest_priority_member_of_the_served_set() -> None:
    votes = [
        ConsensusVote("outlier", 0, Decimal("110")),
        ConsensusVote("second", 1, Decimal("100")),
        ConsensusVote("third", 2, Decimal("100")),
        ConsensusVote("fourth", 3, Decimal("100")),
    ]
    assert grade_consensus(votes).served_group_id == "second"
    assert grade_consensus(votes[:2]).served_group_id == "outlier"


def test_the_tolerance_is_one_tenth_of_one_percent() -> None:
    assert CONSENSUS_RELATIVE_TOLERANCE == Decimal("0.001")


def test_a_group_votes_once() -> None:
    with pytest.raises(ValueError, match="votes once"):
        grade_consensus([ConsensusVote("a", 0, Decimal(1)), ConsensusVote("a", 1, Decimal(1))])


@pytest.mark.parametrize(
    ("band", "graded", "expected"),
    [
        (HIGH, Decimal("0.85"), Decimal("1.00")),
        (MEDIUM, Decimal("0.85"), Decimal("0.85")),
        (LOW, Decimal("0.65"), Decimal("0.65")),
        (LOW, Decimal("1.0"), Decimal("0.99")),
    ],
)
def test_numeric_confidence_is_one_for_high_and_graded_below_it(
    band: ConsensusBand, graded: Decimal, expected: Decimal
) -> None:
    assert consensus_confidence(band, graded) == expected


# -- the engine applies the rule ------------------------------------------------------------------

CUTOFF = datetime(2026, 9, 17, tzinfo=UTC)
PRIORITY = ("yahoo:v1", "twelve:v1", "moomoo:v1", "fourth:v1")


def _policy() -> ReconciliationPolicy:
    return ReconciliationPolicy(
        policy_version="price-consensus:test",
        source_priority=PRIORITY,
        absolute_tolerance=Decimal(0),
        relative_tolerance=CONSENSUS_RELATIVE_TOLERANCE,
        conflict_behavior=ConflictBehavior.MEDIAN_CONSENSUS,
    )


def _cell() -> ReconciliationCell:
    return ReconciliationCell(
        requirement_id=f"data-requirement:{'a' * 64}",
        subject=SubjectRef(kind=SubjectKind.LISTING, id="listing:xnas:avgo"),
        field_name="open",
        field_semantics_id=f"field-semantics:{'b' * 64}",
        unit="USD",
        valid_from=date(2026, 9, 16),
        valid_to=date(2026, 9, 16),
    )


def _assertions(cell: ReconciliationCell, values: dict[str, str | None]) -> tuple[SourceAssertion, ...]:
    out = []
    for index, (source, value) in enumerate(values.items()):
        digest = f"{index + 1:x}" * 64
        out.append(
            SourceAssertion(
                cell_id=cell.cell_id,
                observation_id=f"normalized-observation:{digest}",
                source_id=source,
                origin_group_id=f"origin:{source}",
                knowable_at=CUTOFF,
                normalized_value_sha256=digest if value is None or not value.startswith("cat:") else "e" * 64,
                numeric_value=None if value is None or value.startswith("cat:") else Decimal(value),
                confidence_assessment_id=f"confidence-assessment:{digest}",
                confidence_score=Decimal("0.85"),
                lineage_node_ids=(f"raw-object:{digest}",),
                lineage_complete=True,
            )
        )
    return tuple(out)


def _reconcile(values: dict[str, str | None]):
    cell = _cell()
    assertions = _assertions(cell, values)
    result = reconcile_source_assertions(cell=cell, assertions=assertions, policy=_policy(), cutoff=CUTOFF)
    by_id = {item.assertion_id: item.source_id for item in assertions}
    return result, by_id


def test_engine_high_with_an_outlier_serves_the_median_at_full_confidence() -> None:
    result, by_id = _reconcile(
        {"yahoo:v1": "342.72", "twelve:v1": "342.72000", "moomoo:v1": "342.365", "fourth:v1": "342.70"}
    )
    assert result.band is HIGH and result.outcome is ReconciliationOutcome.AGREED
    assert result.selected_numeric_value == Decimal("342.72")
    assert result.selected_confidence_score == HIGH_CONSENSUS_CONFIDENCE
    assert by_id[result.selected_assertion_id] == "yahoo:v1"
    assert [by_id[item] for item in result.conflicting_assertion_ids] == ["moomoo:v1"]
    assert "reconciliation.outlier_recorded" in result.reason_codes


def test_engine_medium_pair_is_agreed_and_serves_the_mean() -> None:
    result, by_id = _reconcile({"yahoo:v1": "100", "twelve:v1": "100.1"})
    assert (result.band, result.outcome) == (MEDIUM, ReconciliationOutcome.AGREED)
    assert result.selected_numeric_value == Decimal("100.05")
    assert result.selected_confidence_score == Decimal("0.85")


def test_engine_low_conflict_serves_the_priority_value_and_records_the_disagreement() -> None:
    result, by_id = _reconcile({"yahoo:v1": "316.85", "twelve:v1": "317.80", "moomoo:v1": "318.80"})
    assert (result.band, result.outcome) == (LOW, ReconciliationOutcome.CONFLICT_PRIORITY_SERVED)
    assert by_id[result.selected_assertion_id] == "yahoo:v1"
    assert result.selected_numeric_value == Decimal("316.85")
    assert sorted(by_id[item] for item in result.conflicting_assertion_ids) == ["moomoo:v1", "twelve:v1"]


def test_engine_single_origin_is_low_and_insufficient() -> None:
    result, _ = _reconcile({"twelve:v1": "10"})
    assert (result.band, result.outcome) == (LOW, ReconciliationOutcome.INSUFFICIENT_INDEPENDENT_ORIGINS)


def test_engine_categorical_values_agree_only_when_equal() -> None:
    agreed, _ = _reconcile({"yahoo:v1": "cat:member", "twelve:v1": "cat:member", "moomoo:v1": "cat:member"})
    assert agreed.band is HIGH and agreed.selected_numeric_value is None
    mixed, _ = _reconcile({"yahoo:v1": "cat:member", "twelve:v1": "5"})
    assert mixed.band is LOW


def test_a_median_consensus_policy_corroborates_at_two_groups_only() -> None:
    with pytest.raises(ValidationError, match="exactly two"):
        ReconciliationPolicy(
            policy_version="price-consensus:test",
            source_priority=PRIORITY,
            absolute_tolerance=Decimal(0),
            relative_tolerance=CONSENSUS_RELATIVE_TOLERANCE,
            minimum_independent_origin_groups=3,
            conflict_behavior=ConflictBehavior.MEDIAN_CONSENSUS,
        )


def test_a_result_cannot_claim_a_band_its_partition_does_not_support() -> None:
    result, _ = _reconcile({"yahoo:v1": "100", "twelve:v1": "100.1"})
    with pytest.raises(ValidationError, match="three agreeing"):
        result.model_validate({**result.model_dump(), "band": "high", "selected_confidence_score": "1.00"})
    high, _ = _reconcile({"yahoo:v1": "100", "twelve:v1": "100", "moomoo:v1": "100"})
    with pytest.raises(ValidationError, match="1.00"):
        high.model_validate({**high.model_dump(), "selected_confidence_score": "0.85"})
    with pytest.raises(ValidationError, match="only a high band"):
        result.model_validate({**result.model_dump(), "selected_confidence_score": "1"})
