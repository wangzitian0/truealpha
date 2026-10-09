"""Status honesty sweep for every mart writer (#1116, init.md section 8).

A mart row carries two status columns that a writer must not guess:

- `source_evidence_status`: `verified` needs a raw pointer chain that dereferences.
  A row with no pointer is `degraded`.
- `factor_validation_status`: `accepted` needs a sealed holdout record.
  `VALIDATION_RECORDS` is empty, so the honest value is `not_evaluated`.

Two layers guard this.

1. A static scan bans the string literal `accepted` in the data-engine source.
   Only `status_dimensions.py` may name it. The validation module lives in `libs/factors`,
   outside the scanned tree.
2. A run of each writer on a recording connection. The test binds the INSERT column
   list to the bound parameters. It compares each status column with the status source.
"""

from __future__ import annotations

import ast
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from data_engine.core_strategy_replay import run as run_strategy_replay
from data_engine.datahub.analyst_ratings import (
    capture_ticker_analyst_ratings,
    materialize_analyst_ratings,
    materialize_universe_analyst_ratings,
)
from data_engine.datahub.analyst_ratings import materialize_unvisited_issuers as unvisited_analyst_ratings
from data_engine.datahub.canonical_issuer import CanonicalIssuer
from data_engine.datahub.production_topt import status_dimensions
from data_engine.datahub.standards.supply_chain_extraction import (
    materialize_supply_chain_exposure,
    materialize_universe_supply_chain_exposure,
)
from data_engine.datahub.standards.supply_chain_extraction import (
    materialize_unvisited_issuers as unvisited_supply_chain,
)
from data_engine.sources import moomoo as moomoo_source
from data_engine.strategy_replay_repository import write_strategy_decision
from factors.base.analyst_track_record import AnalystRatingItem, analyst_track_record
from factors.base.supply_chain_exposure import SupplyChainPartner, supply_chain_exposure
from factors.validation_records import validation_status_for
from truealpha_contracts.execution import FactorValidationStatus, InputEvidenceStatus

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_ENGINE_SRC = REPO_ROOT / "apps" / "data-engine" / "src"

#: The one data-engine module that may name the `accepted` status. It holds the status source.
STATUS_SOURCE_FILES = {"apps/data-engine/src/data_engine/datahub/production_topt/status_dimensions.py"}

#: Writers that must appear in the scanned tree. A scan that misses them proves nothing.
SWEPT_WRITER_FILES = {
    "apps/data-engine/src/data_engine/datahub/analyst_ratings.py",
    "apps/data-engine/src/data_engine/datahub/standards/supply_chain_extraction.py",
    "apps/data-engine/src/data_engine/strategy_replay_repository.py",
}

#: A SQL statement that names the status as a quoted literal, for example `... 'accepted')`.
_QUOTED_ACCEPTED_SQL = re.compile(r"(?<!\w)'accepted'(?!\w)")


def _src_python_files() -> list[Path]:
    """Every production module under `apps/data-engine/src`. No tests, no `__pycache__`."""
    files: list[Path] = []
    for path in DATA_ENGINE_SRC.rglob("*.py"):
        parts = path.relative_to(REPO_ROOT).parts
        if "tests" in parts or "__pycache__" in parts:
            continue
        files.append(path)
    return sorted(files)


def _is_accepted_enum_member(node: ast.AST) -> bool:
    """True for `FactorValidationStatus.ACCEPTED`, also behind a module chain such as `execution.`."""
    if not isinstance(node, ast.Attribute) or node.attr != "ACCEPTED":
        return False
    owner = node.value
    return (isinstance(owner, ast.Name) and owner.id == "FactorValidationStatus") or (
        isinstance(owner, ast.Attribute) and owner.attr == "FactorValidationStatus"
    )


def _accepted_literal_lines(source: str, filename: str) -> list[int]:
    """Line numbers that write the `accepted` status.

    Three shapes count: a string constant that is `accepted`, a string that quotes `'accepted'`
    in SQL, and the `ACCEPTED` member of `FactorValidationStatus`.
    """
    tree = ast.parse(source, filename=filename)
    lines: list[int] = []
    for node in ast.walk(tree):
        if _is_accepted_enum_member(node):
            lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.strip() == "accepted" or _QUOTED_ACCEPTED_SQL.search(node.value):
                lines.append(node.lineno)
    return sorted(lines)


def _scan_accepted_literal(files: list[Path], *, allowed: set[str]) -> list[str]:
    violations: list[str] = []
    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in allowed:
            continue
        for line in _accepted_literal_lines(path.read_text(), rel):
            violations.append(f"{rel}:{line} writes the status 'accepted'. Take the status from status_dimensions.")
    return violations


def test_the_scan_covers_every_swept_writer_file() -> None:
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in _src_python_files()}
    assert SWEPT_WRITER_FILES <= scanned, f"the scan misses {sorted(SWEPT_WRITER_FILES - scanned)}"
    assert STATUS_SOURCE_FILES <= scanned, "the status source is not in the scanned tree"


def test_no_data_engine_module_writes_the_accepted_literal_outside_the_status_source() -> None:
    violations = _scan_accepted_literal(_src_python_files(), allowed=STATUS_SOURCE_FILES)
    assert not violations, "\n".join(violations)


def test_the_accepted_literal_scan_fires_on_each_hard_coded_shape(tmp_path: Path) -> None:
    """Red proof: the scan flags a plain constant, a dict default, an f-string part, SQL text
    and the `ACCEPTED` attribute of `FactorValidationStatus`, by name or through a module chain."""
    rogue = tmp_path / "rogue_writer.py"
    rogue.write_text(
        'val_status = "accepted"\n'
        'row = {"factor_validation_status": "accepted"}\n'
        'value = item.get("factor_validation_status", "accepted" if ok else "not_evaluated")\n'
        'text = f"accepted{suffix}"\n'
        "sql = \"insert into t (s) values ('accepted')\"\n"
        'prose = "the accepted fusion engine"\n'
        'table = "staging.accepted_ruleset_head"\n'
        "via_name = FactorValidationStatus.ACCEPTED.value\n"
        "via_chain = execution.FactorValidationStatus.ACCEPTED\n"
        "other_enum = ReviewOutcome.ACCEPTED\n"
        "other_member = FactorValidationStatus.NOT_EVALUATED\n"
    )
    lines = _accepted_literal_lines(rogue.read_text(), "rogue_writer.py")
    assert lines == [1, 2, 3, 4, 5, 8, 9], (
        "prose, table names, other enums and other members of the status enum must not count"
    )


# --- layer 2: run each writer on a recording connection


class _Cursor:
    """Answers like a cursor over a graph that holds one supplier edge for every issuer.

    A statement that writes a row reports one written row. The graph read returns one partner.
    """

    rowcount = 1

    def __init__(self, sql: str = "") -> None:
        self._sql = sql

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        self._sql = sql
        return self

    def fetchone(self) -> tuple[Any]:
        if "to_regclass" in self._sql:
            return ("staging.kg_edges",)
        if self._sql.lstrip().startswith("select exists"):
            return (True,)
        return ("inserted",)

    def fetchall(self) -> list[tuple[Any, ...]]:
        if "from staging.kg_edges e" in self._sql:
            return [("partner:1", "Partner One", Decimal("0.9"))]
        return []


class _RecordingConnection:
    """Records each statement it receives."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        self.executed.append((sql, params))
        return _Cursor(sql)

    def cursor(self) -> _Cursor:
        return _Cursor()


_INSERT_SHAPE = re.compile(
    r"insert\s+into\s+(?P<table>[\w.]+)\s*\((?P<columns>[^)]*)\)\s*values\s*\((?P<values>[^)]*)\)",
    re.IGNORECASE | re.DOTALL,
)
_STATUS_TABLES = {"mart.issuer_analyst_ratings", "mart.issuer_supply_chain_exposure", "mart.strategy_decisions"}


def _written_rows(connection: _RecordingConnection) -> list[tuple[str, dict[str, Any]]]:
    """Each status-bearing INSERT as (table, column to value). It binds `%s` to params in order.

    A quoted SQL literal in the VALUES list is a value of its own. The check fails when the
    column count differs from the value count or when a parameter stays unbound.
    """
    rows: list[tuple[str, dict[str, Any]]] = []
    for sql, params in connection.executed:
        shape = _INSERT_SHAPE.search(sql)
        if shape is None or shape["table"] not in _STATUS_TABLES:
            continue
        columns = [column.strip() for column in shape["columns"].split(",")]
        tokens = [token.strip() for token in shape["values"].split(",")]
        assert len(columns) == len(tokens), f"{shape['table']}: {len(columns)} columns, {len(tokens)} values"
        bound = iter(params)
        row = {column: next(bound) if token == "%s" else token.strip("'") for column, token in zip(columns, tokens)}
        assert list(bound) == [], f"{shape['table']}: a parameter has no column"
        rows.append((shape["table"], row))
    return rows


def _issuer_id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://truealpha.invalid/test/issuer/{name}"))


_CUTOFF = datetime(2026, 10, 9, tzinfo=UTC)
_CLAIMS = {"source_evidence_status": "verified", "factor_validation_status": "accepted"}
_CONSENSUS = {"rating": 4, "total": 20, "buy": 80, "hold": 15, "sell": 5}


def _moomoo_consensus(ctx: Any, code: str, caller: str) -> dict[str, int] | None:
    """The vendor stub: AAPL answers, AMD has no coverage, NVDA raises like a dropped socket."""
    if code == "US.NVDA":
        raise ConnectionError("no quote right")
    return None if code == "US.AMD" else dict(_CONSENSUS)


def _drive_q4_dict_rows(connection: _RecordingConnection) -> tuple[str, ...]:
    materialize_analyst_ratings(
        connection,
        run_id="run:honesty",
        cutoff=_CUTOFF,
        ratings_data=[
            {"issuer_id": _issuer_id("a"), "consensus_rating": Decimal("4.5"), "analysts_count": 32},
            {"issuer_id": _issuer_id("b"), "consensus_rating": None},
            {"issuer_id": _issuer_id("c"), "consensus_rating": Decimal("4"), **_CLAIMS},
            {"issuer_id": _issuer_id("d"), "ratings": [AnalystRatingItem("analyst:1", 5, confidence=Decimal("0.9"))]},
        ],
    )
    return ()


def _drive_q4_factor_records(connection: _RecordingConnection) -> tuple[str, ...]:
    covered = analyst_track_record(
        [AnalystRatingItem("analyst:1", 5, confidence=Decimal("0.9"))], entity_id=_issuer_id("e"), as_of=_CUTOFF
    )
    uncovered = analyst_track_record([], entity_id=_issuer_id("f"), as_of=_CUTOFF)
    materialize_analyst_ratings(connection, run_id="run:honesty", cutoff=_CUTOFF, ratings_data=[covered, uncovered])
    return ()


def _drive_q4_capture(connection: _RecordingConnection) -> tuple[str, ...]:
    capture_ticker_analyst_ratings(
        object(), ticker="AAPL", company_id=_issuer_id("g"), connection=connection, run_id="run:honesty", cutoff=_CUTOFF
    )
    capture_ticker_analyst_ratings(
        None,
        ticker="MSFT",
        company_id=_issuer_id("h"),
        connection=connection,
        run_id="run:honesty",
        cutoff=_CUTOFF,
        open_error=RuntimeError("OpenD is down"),
    )
    return ()


def _drive_q4_universe(connection: _RecordingConnection) -> tuple[str, ...]:
    materialize_universe_analyst_ratings(
        connection,
        run_id="run:honesty",
        cutoff=_CUTOFF,
        tickers={_issuer_id("i"): "AAPL", _issuer_id("j"): "NVDA", _issuer_id("k"): "AMD"},
        ctx=object(),
    )
    return ()


def _drive_q4_unvisited(connection: _RecordingConnection) -> tuple[str, ...]:
    unvisited_analyst_ratings(
        connection, run_id="run:honesty", cutoff=_CUTOFF, unvisited=[(_issuer_id("l"), "head_member_not_in_universe")]
    )
    return ()


def _drive_q3_dict_rows(connection: _RecordingConnection) -> tuple[str, ...]:
    materialize_supply_chain_exposure(
        connection,
        run_id="run:honesty",
        cutoff=_CUTOFF,
        exposure_data=[
            {"issuer_id": _issuer_id("m"), "exposure_score": Decimal("0.85"), "direct_partners": 12},
            {"issuer_id": _issuer_id("n"), "exposure_score": None},
            {"issuer_id": _issuer_id("o"), "exposure_score": Decimal("0.4"), **_CLAIMS},
            {"issuer_id": _issuer_id("p"), "partners": [], "supplies_to_edges_exist": True},
        ],
    )
    return ()


def _drive_q3_factor_records(connection: _RecordingConnection) -> tuple[str, ...]:
    partners = [
        SupplyChainPartner("p:tsmc", "TSMC", "supplier", revenue_share=Decimal("0.5"), confidence=Decimal("0.9"))
    ]
    covered = supply_chain_exposure(partners, entity_id=_issuer_id("q"), as_of=_CUTOFF, supplies_to_edges_exist=True)
    uncovered = supply_chain_exposure([], entity_id=_issuer_id("r"), as_of=_CUTOFF, supplies_to_edges_exist=False)
    materialize_supply_chain_exposure(
        connection, run_id="run:honesty", cutoff=_CUTOFF, exposure_data=[covered, uncovered]
    )
    return ()


def _drive_q3_universe(connection: _RecordingConnection) -> tuple[str, ...]:
    issuers = [
        CanonicalIssuer(issuer_id=_issuer_id("s"), legacy_id="issuer:lei:AAAAAAAAAAAAAAAAAA01", ticker="NVDA"),
        CanonicalIssuer(issuer_id=_issuer_id("t"), legacy_id="issuer:lei:BBBBBBBBBBBBBBBBBB02", ticker="AAPL"),
    ]
    materialize_universe_supply_chain_exposure(connection, run_id="run:honesty", cutoff=_CUTOFF, issuers=issuers)
    return ()


def _drive_q3_unvisited(connection: _RecordingConnection) -> tuple[str, ...]:
    unvisited_supply_chain(
        connection, run_id="run:honesty", cutoff=_CUTOFF, unvisited=[(_issuer_id("u"), "head_member_not_in_universe")]
    )
    return ()


def _drive_strategy_decisions(connection: _RecordingConnection) -> tuple[str, ...]:
    decisions, definition = run_strategy_replay()
    for decision in decisions:
        write_strategy_decision(connection, decision, strategy_run_id="strategy-run:honesty", definition=definition)
    return (f"{definition.strategy_id}:{definition.content_sha256}",)


@dataclass(frozen=True)
class WriterCase:
    """One mart writer entry point and the number of status rows it writes."""

    drive: Callable[[_RecordingConnection], tuple[str, ...]]
    table: str
    row_count: int
    #: False when the statement holds the status as an SQL literal. The status source cannot reroute it.
    routed: bool = True


WRITER_CASES = {
    "q4-dict-rows": WriterCase(_drive_q4_dict_rows, "mart.issuer_analyst_ratings", 4),
    "q4-factor-records": WriterCase(_drive_q4_factor_records, "mart.issuer_analyst_ratings", 2),
    "q4-capture-ticker": WriterCase(_drive_q4_capture, "mart.issuer_analyst_ratings", 2),
    "q4-universe": WriterCase(_drive_q4_universe, "mart.issuer_analyst_ratings", 3),
    "q4-unvisited-fill": WriterCase(_drive_q4_unvisited, "mart.issuer_analyst_ratings", 1, routed=False),
    "q3-dict-rows": WriterCase(_drive_q3_dict_rows, "mart.issuer_supply_chain_exposure", 4),
    "q3-factor-records": WriterCase(_drive_q3_factor_records, "mart.issuer_supply_chain_exposure", 2),
    "q3-universe": WriterCase(_drive_q3_universe, "mart.issuer_supply_chain_exposure", 2),
    "q3-unvisited-fill": WriterCase(_drive_q3_unvisited, "mart.issuer_supply_chain_exposure", 1, routed=False),
    "strategy-decisions": WriterCase(_drive_strategy_decisions, "mart.strategy_decisions", 10),
}


def _run_case(case: WriterCase, monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    monkeypatch.setattr(moomoo_source, "get_analyst_consensus", _moomoo_consensus)
    connection = _RecordingConnection()
    definition_ids = case.drive(connection)
    rows = [row for table, row in _written_rows(connection) if table == case.table]
    assert len(rows) == case.row_count, f"expected {case.row_count} rows in {case.table}, found {len(rows)}"
    return rows, definition_ids


@pytest.mark.parametrize("name", list(WRITER_CASES))
def test_each_writer_row_carries_the_honest_statuses(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """No raw pointer reaches these writers, so evidence is `degraded`. No sealed record exists,
    so validation follows the registry. A caller that claims `verified` or `accepted` gets no effect."""
    rows, definition_ids = _run_case(WRITER_CASES[name], monkeypatch)
    expected_validation = validation_status_for(definition_ids).value
    assert expected_validation == FactorValidationStatus.NOT_EVALUATED.value, "the registry holds no verdict today"
    for row in rows:
        assert row["source_evidence_status"] == InputEvidenceStatus.DEGRADED.value, row
        assert row["factor_validation_status"] == expected_validation, row


@pytest.mark.parametrize("name", [name for name, case in WRITER_CASES.items() if case.routed])
def test_each_writer_takes_the_validation_status_from_the_status_source(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red proof against a hard-coded `not_evaluated`: change the verdict and the row follows."""
    monkeypatch.setattr(
        status_dimensions, "validation_status_for", lambda definition_ids: FactorValidationStatus.REJECTED
    )
    rows, _ = _run_case(WRITER_CASES[name], monkeypatch)
    for row in rows:
        assert row["factor_validation_status"] == FactorValidationStatus.REJECTED.value, row


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("q4-dict-rows", ["available", "unavailable", "available", "available"]),
        ("q3-dict-rows", ["available", "unavailable", "available", "unavailable"]),
        ("q3-universe", ["available", "available"]),
    ],
)
def test_a_row_keeps_its_availability_when_the_statuses_change(
    name: str, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1116 acceptance: status honesty must not change `answered`. An available row stays available."""
    rows, _ = _run_case(WRITER_CASES[name], monkeypatch)
    assert [row["availability_status"] for row in rows] == expected
