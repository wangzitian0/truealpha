"""Weekly question-coverage report (#748, init.md rule 24).

Expected side: `truealpha_contracts.question_requirements` — per §0 question, the wide-row
columns that answer it. Observed side: the governed head's factor rows with their §8 status
dimensions (#747). The report left-joins the two per (question, issuer) and counts
`answered` / `unavailable:<reason>` / `missing`, where `missing` means no column exists for
this universe yet — a gap owned by an issue, never folded into "unavailable".
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.question_requirements import (
    QUESTION_REQUIREMENTS,
    QUESTION_REQUIREMENTS_SHA256,
    QUESTION_TEXT,
    FactorColumn,
    QuestionRequirement,
)

#: The backfill lane names universes by their list; the governed pointer names them by
#: `universe:<list>-<region>-<partition>`. The prefix picks the newest head for the list.
UNIVERSE_PREFIXES: Mapping[str, str] = {"universe-list:qqq": "universe:qqq-", "topt": "universe:topt-"}
GOVERNING_FACTOR = "gross_profit_per_employee"
#: A column that exists but whose row carries no reason yet (#747 scope 3 pending).
UNRECORDED_REASON = "unrecorded_reason"
NO_ROW = "no_row"


@dataclass(frozen=True)
class Cell:
    """One observed (issuer, column) value: answered, or unavailable with a reason."""

    issuer_id: str
    answered: bool
    reason: str | None = None


@dataclass(frozen=True)
class GovernedHead:
    universe_id: str
    run_id: str
    cutoff: datetime


def governed_head(
    connection: Connection[Any], *, universe_prefix: str, environment: str = "production"
) -> GovernedHead | None:
    row = connection.execute(
        """
        select h.universe_id, h.target_run_id, min(r.cutoff)
        from mart.current_pointer_head h
        join mart.topt_gppe_results r on r.run_id = h.target_run_id
        where h.environment = %s and h.factor_id = %s and h.universe_id like %s
        group by h.universe_id, h.target_run_id, h.advanced_at
        order by h.advanced_at desc
        limit 1
        """,
        (environment, GOVERNING_FACTOR, universe_prefix + "%"),
    ).fetchone()
    if row is None:
        return None
    return GovernedHead(universe_id=str(row[0]), run_id=str(row[1]), cutoff=row[2])


def gppe_cells(connection: Connection[Any], run_id: str) -> tuple[Cell, ...]:
    rows = connection.execute(
        """
        select issuer_id, availability_status, availability, reason_codes
        from mart.topt_gppe_results where run_id = %s order by issuer_id
        """,
        (run_id,),
    ).fetchall()
    cells = []
    for issuer_id, availability_status, availability, reason_codes in rows:
        status = availability_status or availability  # rows written before #747 carry only `availability`
        if status == "available":
            cells.append(Cell(str(issuer_id), True))
        else:
            reason = (list(reason_codes or []) or [status or UNRECORDED_REASON])[0]
            cells.append(Cell(str(issuer_id), False, str(reason)))
    return tuple(cells)


def peg_cells(connection: Connection[Any]) -> tuple[Cell, ...]:
    """PEG from the newest strategy run's decisions (TOPT only today)."""
    rows = connection.execute(
        """
        select d.issuer_id, d.peg, d.availability_status, d.exclusion_reason
        from mart.strategy_decisions d
        where d.strategy_run_id = (select strategy_run_id from mart.strategy_runs order by executed_at desc limit 1)
        order by d.issuer_id
        """
    ).fetchall()
    cells = []
    for issuer_id, peg, availability_status, exclusion_reason in rows:
        if peg is not None:
            cells.append(Cell(str(issuer_id), True))
        elif availability_status == "excluded" and exclusion_reason:
            cells.append(Cell(str(issuer_id), False, f"excluded:{exclusion_reason}"))
        else:
            cells.append(Cell(str(issuer_id), False, UNRECORDED_REASON))
    return tuple(cells)


def classify_question(
    requirement: QuestionRequirement,
    *,
    universe_id: str,
    issuers: Iterable[str],
    cells_by_column: Mapping[str, Iterable[Cell]],
) -> dict[str, Any]:
    """Left-join the expected issuers with the observed cells of the question's column.

    `missing` when no column applies to this universe; `unavailable:no_row` when the column
    exists but the issuer has no row (the join produced nothing — the red-proof case).
    """
    expected = tuple(dict.fromkeys(issuers))
    columns = [column for column in requirement.columns if column.applies_to(universe_id)]
    entry: dict[str, Any] = {
        "text": QUESTION_TEXT[requirement.question],
        "tracking_issue": requirement.tracking_issue,
        "denominator": len(expected),
        "column": None,
        "answered": 0,
        "unavailable": {},
        "missing": 0,
    }
    if not columns:
        entry["missing"] = len(expected)
        return entry
    column = columns[0]
    entry["column"] = f"{column.table}.{column.column}"
    observed = {cell.issuer_id: cell for cell in cells_by_column.get(_column_key(column), ())}
    unavailable: Counter[str] = Counter()
    for issuer_id in expected:
        cell = observed.get(issuer_id)
        if cell is None:
            unavailable[NO_ROW] += 1
        elif cell.answered:
            entry["answered"] += 1
        else:
            unavailable[cell.reason or UNRECORDED_REASON] += 1
    entry["unavailable"] = dict(sorted(unavailable.items(), key=lambda item: (-item[1], item[0])))
    return entry


def _column_key(column: FactorColumn) -> str:
    return f"{column.table}.{column.column}"


def compile_report(
    connection: Connection[Any],
    *,
    universe: str,
    executed_at: datetime,
    environment: str = "production",
) -> dict[str, Any] | None:
    """The report for one lane universe, or None when it has no governed head yet."""
    prefix = UNIVERSE_PREFIXES.get(universe, universe)
    head = governed_head(connection, universe_prefix=prefix, environment=environment)
    if head is None:
        return None
    gppe = gppe_cells(connection, head.run_id)
    issuers = [cell.issuer_id for cell in gppe]
    cells_by_column = {
        "mart.topt_gppe_results.gppe": gppe,
        "mart.strategy_decisions.peg": peg_cells(connection) if prefix == "universe:topt-" else (),
    }
    questions = {
        requirement.question.value: classify_question(
            requirement, universe_id=head.universe_id, issuers=issuers, cells_by_column=cells_by_column
        )
        for requirement in QUESTION_REQUIREMENTS
    }
    return {
        "universe": universe,
        "universe_id": head.universe_id,
        "run_id": head.run_id,
        "cutoff": head.cutoff.astimezone(UTC).isoformat(),
        "environment": environment,
        "requirements_sha256": QUESTION_REQUIREMENTS_SHA256,
        "generated_at": executed_at.astimezone(UTC).isoformat(),
        "denominator": len(issuers),
        "questions": questions,
    }


def persist(connection: Connection[Any], report: Mapping[str, Any]) -> str:
    content_sha256 = canonical_sha256(dict(report))
    report_id = f"question-coverage-report:{content_sha256}"
    connection.execute(
        """
        insert into mart.question_coverage_report
            (report_id, content_sha256, universe_id, run_id, cutoff, requirements_sha256, payload)
        values (%s, %s, %s, %s, %s, %s, %s)
        on conflict (report_id) do nothing
        """,
        (
            report_id,
            content_sha256,
            report["universe_id"],
            report["run_id"],
            report["cutoff"],
            report["requirements_sha256"],
            Jsonb(dict(report)),
        ),
    )
    return report_id


def summary_line(report: Mapping[str, Any]) -> str:
    parts = []
    for question, entry in report["questions"].items():
        unavailable = sum(entry["unavailable"].values())
        parts.append(
            f"{question}: {entry['answered']}/{entry['denominator']} answered, {unavailable} unavailable, {entry['missing']} missing"
        )
    return f"{report['universe_id']} @ {report['cutoff']}: " + "; ".join(parts)
