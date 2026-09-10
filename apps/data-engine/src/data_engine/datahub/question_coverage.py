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
    QuestionScope,
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
    """One observed (subject, column) value: answered, or unavailable with a reason.

    `subject_id` is an issuer for an `ISSUER`-scoped question and a fund for a `FUND`-scoped
    one. The field kept its `issuer_id` name until #748 met a question whose subject is not
    an issuer; the name is now the general one, because a lookup keyed by "issuer" that is
    handed funds is the mis-join that makes a registry gap look like a data outage.
    """

    subject_id: str
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
    for subject_id, availability_status, availability, reason_codes in rows:
        status = availability_status or availability  # rows written before #747 carry only `availability`
        if status == "available":
            cells.append(Cell(str(subject_id), True))
        else:
            reason = (list(reason_codes or []) or [status or UNRECORDED_REASON])[0]
            cells.append(Cell(str(subject_id), False, str(reason)))
    return tuple(cells)


def peg_cells(connection: Connection[Any], *, cutoff: datetime) -> tuple[Cell, ...]:
    """PEG from the strategy run whose decisions describe the same cutoff as the governed
    head (the newest run whose decision cutoff is at or before it) — never simply the
    newest run, which could carry a different vintage (Copilot on #779)."""
    rows = connection.execute(
        """
        with chosen as (
            select s.strategy_run_id
            from mart.strategy_runs s
            join mart.strategy_decisions d on d.strategy_run_id = s.strategy_run_id
            where d.cutoff_at <= %s
            group by s.strategy_run_id, s.executed_at
            order by max(d.cutoff_at) desc, s.executed_at desc
            limit 1
        )
        select d.issuer_id, d.peg, d.availability_status, d.exclusion_reason
        from mart.strategy_decisions d
        where d.strategy_run_id = (select strategy_run_id from chosen)
        order by d.issuer_id
        """,
        (cutoff,),
    ).fetchall()
    cells = []
    for subject_id, peg, availability_status, exclusion_reason in rows:
        if peg is not None:
            cells.append(Cell(str(subject_id), True))
        elif availability_status == "excluded" and exclusion_reason:
            cells.append(Cell(str(subject_id), False, f"excluded:{exclusion_reason}"))
        else:
            cells.append(Cell(str(subject_id), False, UNRECORDED_REASON))
    return tuple(cells)


def fund_cells(connection: Connection[Any], run_id: str) -> tuple[Cell, ...]:
    """Module 5's consolidation rows for this run — one per FUND, not per issuer (#36).

    The subject is the fund because the question is about the fund. A refused consolidation
    (coverage below the definition's floors) is `unavailable` with the refusing floor as its
    reason, which is the same shape every issuer-scoped cell uses.
    """
    rows = connection.execute(
        """
        select fund_id, availability_status, reason_codes
        from mart.fund_virtual_company where run_id = %s order by fund_id
        """,
        (run_id,),
    ).fetchall()
    cells = []
    for fund_id, availability_status, reason_codes in rows:
        if availability_status == "available":
            cells.append(Cell(str(fund_id), True))
        else:
            reason = (list(reason_codes or []) or [availability_status or UNRECORDED_REASON])[0]
            cells.append(Cell(str(fund_id), False, str(reason)))
    return tuple(cells)


def classify_question(
    requirement: QuestionRequirement,
    *,
    universe_id: str,
    issuers: Iterable[str],
    cells_by_column: Mapping[str, Iterable[Cell]],
    funds: Iterable[str] = (),
) -> dict[str, Any]:
    """Left-join the expected SUBJECTS with the observed cells of the question's columns.

    The subject set comes from the requirement's scope: issuers for an `ISSUER` question,
    funds for a `FUND` one. Before #748 met a fund-scoped question this was always the
    issuer list, which is why q5 could not be registered at all — its one row would have
    been looked up under twenty issuer ids and graded `unavailable:no_row` twenty times, a
    red describing the registry rather than the data.

    `missing` when no column applies to this universe; a subject is `answered` when ANY
    applicable column answers it; otherwise the first column with a row supplies the
    reason; `unavailable:no_row` when no column has a row for the subject at all (the join
    produced nothing — the red-proof case).
    """
    subjects = funds if requirement.scope is QuestionScope.FUND else issuers
    expected = tuple(dict.fromkeys(subjects))
    columns = [column for column in requirement.columns if column.applies_to(universe_id)]
    entry: dict[str, Any] = {
        "text": QUESTION_TEXT[requirement.question],
        "tracking_issue": requirement.tracking_issue,
        # Named on the row so a reader never has to infer why q5's denominator is 1 while
        # q1's is 20 — two different subjects, not a truncated count.
        "scope": requirement.scope.value,
        "denominator": len(expected),
        "column": None,
        "columns": [_column_key(column) for column in columns],
        "answered": 0,
        "unavailable": {},
        "missing": 0,
    }
    if not columns:
        entry["missing"] = len(expected)
        return entry
    entry["column"] = entry["columns"][0]
    observed = [{cell.subject_id: cell for cell in cells_by_column.get(_column_key(column), ())} for column in columns]
    unavailable: Counter[str] = Counter()
    for subject_id in expected:
        cells = [lookup[subject_id] for lookup in observed if subject_id in lookup]
        if not cells:
            unavailable[NO_ROW] += 1
        elif any(cell.answered for cell in cells):
            entry["answered"] += 1
        else:
            unavailable[cells[0].reason or UNRECORDED_REASON] += 1
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
    issuers = [cell.subject_id for cell in gppe]
    # The fund subjects are whatever module 5 consolidated for THIS run — an empty tuple on
    # a universe whose tick does not consolidate, which grades q5 `missing` there rather
    # than borrowing another universe's funds.
    funds_observed = fund_cells(connection, head.run_id)
    funds = [cell.subject_id for cell in funds_observed]
    cells_by_column = {
        "mart.topt_gppe_results.gppe": gppe,
        "mart.strategy_decisions.peg": peg_cells(connection, cutoff=head.cutoff) if prefix == "universe:topt-" else (),
        "mart.fund_virtual_company.weighted_valuation_gap": funds_observed,
    }
    questions = {
        requirement.question.value: classify_question(
            requirement,
            universe_id=head.universe_id,
            issuers=issuers,
            funds=funds,
            cells_by_column=cells_by_column,
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
