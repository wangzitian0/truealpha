"""The goal-criteria catalog and the checks that back it agree in both directions (#28 D1).

The standing test reads the real catalog, `tools/nightly_verdicts.json` and every
``pytest.mark.criterion`` marker in the test paths of the root `pyproject.toml`. The other
tests change one fact and assert the named finding, so each direction can fail.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from collections import defaultdict
from pathlib import Path

import pytest
from pydantic import ValidationError
from truealpha_contracts.goal_criteria import (
    ROOTS,
    GoalCriteriaCatalog,
    backing_findings,
    load_goal_criteria,
)
from truealpha_runtime.testing import load_tool  # noqa: I001

REPO = Path(__file__).resolve().parents[3]


def _verdict_keys() -> set[str]:
    """The checks the tool expects: the per-universe checks expanded over the contract of this checkout."""
    return set(load_tool("nightly_verdicts").load_expectations())


def _test_paths() -> list[Path]:
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    return [REPO / path for path in config["tool"]["pytest"]["ini_options"]["testpaths"]]


def _criterion_markers(source: str) -> set[str]:
    """The ids of every ``pytest.mark.criterion("<id>")`` call in ``source``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "criterion"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "mark"
        ):
            for argument in node.args:
                if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
                    raise ValueError(f"criterion marker argument is not a string literal: {ast.unparse(argument)}")
                found.add(argument.value)
    return found


def _markers() -> dict[str, set[str]]:
    markers: dict[str, set[str]] = defaultdict(set)
    for root in _test_paths():
        for path in root.rglob("*.py"):
            for criterion_id in _criterion_markers(path.read_text()):
                markers[criterion_id].add(path.relative_to(REPO).as_posix())
    return dict(markers)


def _payload() -> dict:
    return load_goal_criteria().model_dump(mode="json")


def _with(criteria: list[dict]) -> GoalCriteriaCatalog:
    return GoalCriteriaCatalog.model_validate({**_payload(), "criteria": criteria})


# -- the standing check ------------------------------------------------------------------------


def test_the_catalog_and_its_backing_checks_agree() -> None:
    assert backing_findings(load_goal_criteria(), verdict_keys=_verdict_keys(), markers=_markers()) == []


def test_every_root_lists_its_acceptance_ids_without_a_gap() -> None:
    numbers: dict[str, list[int]] = defaultdict(list)
    for criterion in load_goal_criteria().criteria:
        match = re.match(r"^R(\d)\.[A-E](\d+)$", criterion.id)
        if match:
            numbers[match[1]].append(int(match[2]))
    for root in ROOTS:
        found = sorted(numbers[str(root)])
        assert found == list(range(1, len(found) + 1)), f"R{root}: {found}"
    # The acceptance tables of #1417 to #1421 on 2026-10-20: A1-A8, B1-B10, C1-C7, D1-D6, E1-E10.
    # B11 joined on 2026-10-20 with the runtime-shape verdict of the code server (#1467).
    # B12 (the S&P 500 membership from the IVV holdings) and D7 (the backtest window) joined on 2026-10-21.
    assert {root: len(found) for root, found in numbers.items()} == {"1": 8, "2": 12, "3": 7, "4": 7, "5": 10}


def _criterion(criterion_id: str):
    return load_goal_criteria().by_id()[criterion_id]


def test_the_m0_of_root_two_is_topt_and_qqq_and_the_s_and_p_500_is_the_next_tier() -> None:
    """Owner decision of 2026-10-21: production runs TOPT and QQQ first, each key fetched once."""
    m0, b1 = _criterion("R2.M0"), _criterion("R2.B1")

    assert m0.tier == "M0" and "TOPT and QQQ" in m0.statement and "fetch key is fetched once" in m0.statement
    assert "S&P 500" not in m0.statement, "the S&P 500 is the next tier, not the M0"
    assert b1.tier == "M1" and "S&P 500" in b1.statement and "next production tier" in b1.statement


def test_the_s_and_p_500_membership_comes_from_the_ivv_holdings_and_the_filing_is_the_oracle() -> None:
    criterion = _criterion("R2.B12")

    assert "IVV N-PORT holdings" in criterion.statement
    assert (criterion.tier, criterion.status, criterion.oracle) == ("M1", "pending", "filing")


def test_the_backtest_window_is_a_pending_m1_criterion_and_the_history_floor_of_d5_stays() -> None:
    """Owner decision of 2026-10-21: the window is a parameter of the definition; 12 quarterly cutoffs now, 20 years
    ideal. D5 (at least 60 months of history) is a separate floor and stays pending and unchanged."""
    window, history = _criterion("R4.D7"), _criterion("R4.D5")

    assert (window.tier, window.status) == ("M1", "pending")
    assert "parameter of the strategy definition" in window.statement
    assert "12 quarterly cutoffs" in window.statement and "240 months" in window.statement
    assert (history.tier, history.status, history.threshold) == ("M1", "pending", "at least 60 months")
    assert history.statement.startswith("Backtest history covers at least 60 months")


def test_the_seventy_percent_rule_is_not_restated_in_the_data_files() -> None:
    """#1484: the rule lives in `UTILIZATION_CEILING`. A data file points to it and states no number."""
    data = REPO / "common" / "contracts" / "src" / "truealpha_contracts" / "data"
    criteria = (data / "goal_criteria.v1.json").read_text()
    capacity = json.dumps(json.loads((data / "environment_contract.v1.json").read_text())["code_server_capacity"])
    rule = re.compile(r"\b70 ?%|\b0\.7\b|utilization_limit")

    assert rule.search(criteria) is None
    assert rule.search(capacity) is None
    assert "UTILIZATION_CEILING" in criteria and "UTILIZATION_CEILING" in capacity


def test_the_fixture_reach_test_backs_r5_e2() -> None:
    assert _markers()["R5.E2"] == {"common/runtime/tests/test_no_fixture_in_deployed_code.py"}


def test_the_marker_scan_finds_a_marker_and_refuses_a_computed_id() -> None:
    source = 'import pytest\n\n@pytest.mark.criterion("R3.C5")\ndef test_x():\n    pass\n'
    assert _criterion_markers(source) == {"R3.C5"}
    assert _criterion_markers('pytestmark = pytest.mark.criterion("R4.D6")\n') == {"R4.D6"}
    with pytest.raises(ValueError, match="not a string literal"):
        _criterion_markers('ID = "R3.C5"\npytestmark = pytest.mark.criterion(ID)\n')


# -- each direction can fail -------------------------------------------------------------------


def test_a_verdict_key_that_does_not_exist_fails() -> None:
    criteria = _payload()["criteria"]
    target = next(c for c in criteria if c["verdict_keys"])
    target["verdict_keys"] = [*target["verdict_keys"], "no_such_verdict"]
    findings = backing_findings(_with(criteria), verdict_keys=_verdict_keys(), markers=_markers())
    assert findings == [f"{target['id']}: verdict key 'no_such_verdict' is not in nightly_verdicts.json"]


def test_a_verdict_key_that_backs_no_criterion_fails() -> None:
    findings = backing_findings(
        load_goal_criteria(), verdict_keys=_verdict_keys() | {"orphan_check"}, markers=_markers()
    )
    assert findings == ["verdict key 'orphan_check' backs no catalog criterion"]


def test_a_marker_that_names_no_criterion_fails() -> None:
    markers = {**_markers(), "R9.Z1": {"apps/data-engine/tests/test_x.py"}}
    findings = backing_findings(load_goal_criteria(), verdict_keys=_verdict_keys(), markers=markers)
    assert findings == ["test marker criterion('R9.Z1') names no catalog criterion (apps/data-engine/tests/test_x.py)"]


def test_a_pending_criterion_that_a_marker_backs_fails() -> None:
    catalog = load_goal_criteria()
    pending = next(c for c in catalog.criteria if c.status == "pending")
    markers = {**_markers(), pending.id: {"apps/data-engine/tests/test_x.py"}}
    findings = backing_findings(catalog, verdict_keys=_verdict_keys(), markers=markers)
    assert findings == [f"{pending.id}: pending, but a check backs it; set the status to backed or partial"]


def test_a_backed_criterion_without_a_check_fails() -> None:
    criteria = _payload()["criteria"]
    target = next(c for c in criteria if c["status"] == "partial" and c["verdict_keys"] == ["nightly_backtest"])
    target["verdict_keys"] = []
    findings = backing_findings(_with(criteria), verdict_keys=_verdict_keys(), markers=_markers())
    assert f"{target['id']}: partial, but no verdict key or test marker backs it" in findings


def test_a_marker_backs_a_criterion_with_no_verdict_key() -> None:
    criteria = _payload()["criteria"]
    target = next(c for c in criteria if c["id"] == "R3.C5")
    target.update(status="backed", pending_on=None)
    catalog = _with(criteria)
    markers = {**_markers(), "R3.C5": {"apps/data-engine/tests/test_x.py"}}
    assert backing_findings(catalog, verdict_keys=_verdict_keys(), markers=markers) == []
    assert backing_findings(catalog, verdict_keys=_verdict_keys(), markers=_markers()) == [
        "R3.C5: backed, but no verdict key or test marker backs it"
    ]


# -- the catalog shape -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"id": "R3.A1"}, "uses the letter C"),
        ({"root": "#1417"}, "is not #1419"),
        ({"tier": "M0"}, "tier M0 is not M1"),
        ({"status": "backed"}, "must not name pending_on"),
        ({"pending_on": None}, "must name pending_on"),
        ({"oracle": "self"}, "oracle"),
    ],
)
def test_a_malformed_criterion_is_refused(change: dict, message: str) -> None:
    criteria = _payload()["criteria"]
    target = next(c for c in criteria if c["id"] == "R3.C5")
    target.update(change)
    with pytest.raises(ValidationError, match=message):
        _with(criteria)


def test_a_duplicate_id_and_a_missing_m0_are_refused() -> None:
    criteria = _payload()["criteria"]
    with pytest.raises(ValidationError, match="named twice"):
        _with([*criteria, criteria[0]])
    with pytest.raises(ValidationError, match="R2 has no M0"):
        _with([c for c in criteria if c["id"] != "R2.M0"])
