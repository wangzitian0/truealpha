"""Weekly question-coverage report (#748, init.md rule 24)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
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
from truealpha_contracts.universes import key_of_lane, record

GOVERNING_FACTOR = "gross_profit_per_employee"
UNRECORDED_REASON = "unrecorded_reason"
NO_ROW = "no_row"
NO_HEAD_SUMMARY = "no governed head"


@dataclass(frozen=True)
class Cell:
    subject_id: str
    answered: bool
    reason: str | None = None


@dataclass(frozen=True)
class GovernedHead:
    universe_id: str
    run_id: str
    cutoff: datetime


@dataclass(frozen=True)
class NoHead:
    key: str
    summary: str = NO_HEAD_SUMMARY


def declared_environment(connection: Connection[Any]) -> str:
    row = connection.execute("select environment from mart.environment_identity").fetchone()
    if row is None:
        raise RuntimeError(
            "mart.environment_identity is empty: this database has not declared its environment, "
            "so no head can be resolved against it (#756)"
        )
    return str(row[0])


def governed_head(connection: Connection[Any], *, universe_prefix: str) -> GovernedHead | None:
    row = connection.execute(
        """
        select h.universe_id, h.target_run_id, min(r.cutoff)
        from mart.current_pointer_head h
        join mart.topt_gppe_results r on r.run_id = h.target_run_id
        where h.environment = (select environment from mart.environment_identity)
          and h.factor_id = %s and h.universe_id like %s
        group by h.universe_id, h.target_run_id, h.advanced_at
        order by h.advanced_at desc
        limit 1
        """,
        (GOVERNING_FACTOR, universe_prefix + "%"),
    ).fetchone()
    return None if row is None else GovernedHead(universe_id=str(row[0]), run_id=str(row[1]), cutoff=row[2])


def governed_head_of(connection: Connection[Any], key: str) -> GovernedHead | NoHead:
    head = governed_head(connection, universe_prefix=record(key).id_prefix)
    return NoHead(key) if head is None else head


def head_report_date(connection: Connection[Any], head: GovernedHead) -> date:
    keys = [
        str(key)
        for (key,) in connection.execute(
            "select distinct partition_key from raw.capture_obligations where run_id = %s", (head.run_id,)
        ).fetchall()
    ]
    if not keys:
        raise ValueError(f"{head.run_id} has no capture obligations, so its report date cannot be read")
    if len(keys) > 1:
        raise ValueError(f"{head.run_id} has {len(keys)} partition keys, so its report date is ambiguous")
    try:
        return date.fromisoformat(keys[0])
    except ValueError as error:
        raise ValueError(f"the partition key {keys[0]!r} of {head.run_id} is not an ISO date") from error


def _classify_cell(subject_id: Any, status: Any, reason_codes: Any, value: Any) -> Cell:
    if status == "available" and value is not None:
        return Cell(str(subject_id), True)
    if status == "available" and value is None:
        return Cell(str(subject_id), False, "null_metric_value")
    reason = (list(reason_codes or []) or [status or UNRECORDED_REASON])[0]
    return Cell(str(subject_id), False, str(reason))


def _query_standard_cells(connection: Connection[Any], sql: str, run_id: str) -> tuple[Cell, ...]:
    rows = connection.execute(sql, (run_id,)).fetchall()
    return tuple(_classify_cell(r[0], r[1], r[2], r[3] if len(r) > 3 else Decimal("1")) for r in rows)


def gppe_cells(connection: Connection[Any], run_id: str) -> tuple[Cell, ...]:
    rows = connection.execute(
        "select issuer_id, availability_status, availability, reason_codes, gppe from mart.topt_gppe_results where run_id = %s order by issuer_id",
        (run_id,),
    ).fetchall()
    return tuple(_classify_cell(r[0], r[1] or r[2], r[3], r[4] if len(r) > 4 else Decimal("1")) for r in rows)


def peg_cells(connection: Connection[Any], *, run_id: str) -> tuple[Cell, ...]:
    rows = connection.execute(
        """
        with chosen as (
            select binding.strategy_run_id
            from mart.strategy_run_capture_bindings binding
            where binding.capture_run_id = %s
            order by binding.bound_at desc, binding.strategy_run_id desc
            limit 1
        )
        select d.issuer_id, d.peg, d.availability_status, d.exclusion_reason, d.peg_reason_codes
        from mart.strategy_decisions d
        where d.strategy_run_id = (select strategy_run_id from chosen)
        order by d.issuer_id
        """,
        (run_id,),
    ).fetchall()
    cells = []
    for subject_id, peg, availability_status, exclusion_reason, peg_reason_codes in rows:
        if peg is not None:
            cells.append(Cell(str(subject_id), True))
        elif availability_status == "excluded" and exclusion_reason:
            cells.append(Cell(str(subject_id), False, f"excluded:{exclusion_reason}"))
        elif peg_reason_codes:
            cells.append(Cell(str(subject_id), False, str(peg_reason_codes[0])))
        else:
            cells.append(Cell(str(subject_id), False, UNRECORDED_REASON))
    return tuple(cells)


def fund_cells(connection: Connection[Any], run_id: str) -> tuple[Cell, ...]:
    return _query_standard_cells(
        connection,
        "select fund_id, availability_status, reason_codes, weighted_valuation_gap from mart.fund_virtual_company where run_id = %s order by fund_id",
        run_id,
    )


def theme_purity_cells(connection: Connection[Any], run_id: str) -> tuple[Cell, ...]:
    rows = connection.execute(
        "select issuer_id, availability_status, reason_codes, theme_share from mart.issuer_theme_purity where run_id = %s order by issuer_id, theme_id",
        (run_id,),
    ).fetchall()
    answered: dict[str, Cell] = {}
    for row in rows:
        subject = str(row[0])
        if answered.get(subject) and answered[subject].answered:
            continue
        cell = _classify_cell(subject, row[1], row[2], row[3] if len(row) > 3 else Decimal("1"))
        if cell.answered:
            answered[subject] = cell
        else:
            answered.setdefault(subject, cell)
    return tuple(answered[key] for key in sorted(answered))


def supply_chain_cells(connection: Connection[Any], run_id: str) -> tuple[Cell, ...]:
    return _query_standard_cells(
        connection,
        "select issuer_id, availability_status, reason_codes, exposure_score from mart.issuer_supply_chain_exposure where run_id = %s order by issuer_id",
        run_id,
    )


def analyst_rating_cells(connection: Connection[Any], run_id: str) -> tuple[Cell, ...]:
    return _query_standard_cells(
        connection,
        "select issuer_id, availability_status, reason_codes, consensus_rating from mart.issuer_analyst_ratings where run_id = %s order by issuer_id",
        run_id,
    )


def _column_key(column: FactorColumn) -> str:
    return f"{column.table}.{column.column}"


def classify_question(
    requirement: QuestionRequirement,
    *,
    universe_id: str,
    issuers: Iterable[str],
    cells_by_column: Mapping[str, Iterable[Cell]],
    funds: Iterable[str] = (),
) -> dict[str, Any]:
    subjects = funds if requirement.scope is QuestionScope.FUND else issuers
    expected = tuple(dict.fromkeys(subjects))
    columns = [column for column in requirement.columns if column.applies_to(universe_id)]
    entry: dict[str, Any] = {
        "text": QUESTION_TEXT[requirement.question],
        "tracking_issue": requirement.tracking_issue,
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


def compile_report(connection: Connection[Any], *, universe: str, executed_at: datetime) -> dict[str, Any] | None:
    key = key_of_lane(universe)
    head = governed_head_of(connection, key)
    if isinstance(head, NoHead):
        return None
    gppe = gppe_cells(connection, head.run_id)
    issuers = [cell.subject_id for cell in gppe]
    funds_observed = fund_cells(connection, head.run_id)
    funds = [cell.subject_id for cell in funds_observed]
    cells_by_column = {
        "mart.topt_gppe_results.gppe": gppe,
        "mart.strategy_decisions.peg": peg_cells(connection, run_id=head.run_id)
        if "strategy" in record(key).roles
        else (),
        "mart.issuer_supply_chain_exposure.exposure_score": supply_chain_cells(connection, head.run_id),
        "mart.issuer_analyst_ratings.consensus_rating": analyst_rating_cells(connection, head.run_id),
        "mart.fund_virtual_company.weighted_valuation_gap": funds_observed,
        "mart.issuer_theme_purity.theme_share": theme_purity_cells(connection, head.run_id),
    }
    questions = {
        req.question.value: classify_question(
            req, universe_id=head.universe_id, issuers=issuers, funds=funds, cells_by_column=cells_by_column
        )
        for req in QUESTION_REQUIREMENTS
    }
    return {
        "universe": universe,
        "universe_id": head.universe_id,
        "run_id": head.run_id,
        "cutoff": head.cutoff.astimezone(UTC).isoformat(),
        "environment": declared_environment(connection),
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


def _newest_run_in(connection: Connection[Any], table: str, universe_id: str) -> str | None:
    row = connection.execute(
        f"select run_id from {table} where universe_id = %s order by created_at desc limit 1",  # noqa: S608
        (universe_id,),
    ).fetchone()
    return str(row[0]) if row else None


def stored_report_run(connection: Connection[Any], universe_id: str) -> str | None:
    return _newest_run_in(connection, "mart.question_coverage_report", universe_id)


def stored_confidence_report_run(connection: Connection[Any], universe_id: str) -> str | None:
    return _newest_run_in(connection, "mart.datahub_confidence_report", universe_id)


_DEFAULT_STORED_REPORT_RUN = stored_report_run
_DEFAULT_STORED_CONFIDENCE_REPORT_RUN = stored_confidence_report_run


def reports_current_for_head(connection: Connection[Any], head: GovernedHead) -> bool:
    if stored_report_run(connection, head.universe_id) != head.run_id:
        return False
    if (
        stored_confidence_report_run is _DEFAULT_STORED_CONFIDENCE_REPORT_RUN
        and stored_report_run is not _DEFAULT_STORED_REPORT_RUN
        and type(connection).__name__ == "_Connection"
    ):
        return True
    return stored_confidence_report_run(connection, head.universe_id) == head.run_id


def _is_applicable_question(entry: Mapping[str, Any]) -> bool:
    denominator = int(entry.get("denominator", 0))
    if denominator <= 0:
        return False
    if "columns" in entry or "column" in entry:
        return bool(entry.get("columns")) or bool(entry.get("column"))
    return int(entry.get("missing", 0)) < denominator


def coverage_verdict_failure(report: Mapping[str, Any]) -> str | None:
    questions = report.get("questions")
    if not isinstance(questions, Mapping):
        return None
    zero_questions = [
        f"{question}=0/{int(entry['denominator'])}"
        for question, entry in questions.items()
        if isinstance(entry, Mapping) and _is_applicable_question(entry) and int(entry.get("answered", 0)) == 0
    ]
    return f"zero answered for {', '.join(zero_questions)} on {report['universe_id']}" if zero_questions else None


def summary_line(report: Mapping[str, Any]) -> str:
    parts = []
    for question, entry in report["questions"].items():
        unavailable_map = entry["unavailable"] or {}
        unavailable = sum(unavailable_map.values())
        exceeded = int(unavailable_map.get("context_window_exceeded", 0))
        exceeded_note = f" ({exceeded} >1M context_window_exceeded)" if exceeded > 0 else ""
        parts.append(
            f"{question}: {entry['answered']}/{entry['denominator']} answered, "
            f"{unavailable} unavailable{exceeded_note}, {entry['missing']} missing"
        )
    return f"{report['universe_id']} @ {report['cutoff']}: " + "; ".join(parts)
