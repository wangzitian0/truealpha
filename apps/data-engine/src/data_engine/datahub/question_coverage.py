"""Weekly question-coverage report (#748, init.md rule 24)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from factors.panel import GPPE_BANKING_TCE_SPEC, GPPE_V0_SPEC
from psycopg import Connection
from truealpha_contracts.question_requirements import (
    QUESTION_REQUIREMENTS,
    QUESTION_REQUIREMENTS_SHA256,
    QUESTION_TEXT,
    FactorColumn,
    QuestionRequirement,
    QuestionScope,
)
from truealpha_contracts.reasons import REGISTRY_SHA256, ReasonClass, ReasonKind, registration
from truealpha_contracts.universes import key_of_lane, record

from data_engine.datahub._helpers import gppe_branch_predicate
from data_engine.datahub.repository import persist_run_report

GOVERNING_FACTOR = "gross_profit_per_employee"
REPORT_COMPILER_VERSION = "v3-actionable-coverage"
REPORT_RULES_TAG = f"{REGISTRY_SHA256[:12]}:{REPORT_COMPILER_VERSION}"
UNRECORDED_REASON = "unrecorded_reason"
NO_ROW = "no_row"
NO_HEAD_SUMMARY = "no governed head"
#: A cell outside the five classes is a violation of layer 1 (#1851): no row, no reason, or a
#: reason the registry does not list.
VIOLATION_NO_ROW, VIOLATION_NO_REASON, VIOLATION_UNREGISTERED = "no_row", "no_reason", "unregistered"
#: The served status says the measure applies, but the reason is of the class `not_applicable`: the
#: producer or the registry is wrong, and the report says so rather than count the cell twice.
VIOLATION_STATUS_CLASS_CONFLICT = "status_class_conflict"
MAX_CHANGED_SUBJECTS_LISTED = 20


@dataclass(frozen=True)
class Cell:
    subject_id: str
    answered: bool
    reason: str | None = None
    not_applicable: bool = False


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
            "mart.environment_identity is empty: this database has not declared its environment, so no head can be resolved against it (#756)"
        )
    return str(row[0])


#: The newest head of one universe prefix that has a capture status. The lateral subquery
#: filters mart.capture_status on the head's run before its GROUP BY, so Postgres reads
#: only that run's obligations by index. A plain join aggregates every run first: on
#: staging that read 16k obligations, spilled to disk and took 128 ms per call (#1699).
GOVERNED_HEAD_SQL = (
    "select h.universe_id, h.target_run_id, cs.cutoff from mart.current_pointer_head h "
    "cross join lateral (select min(s.cutoff) as cutoff from mart.capture_status s "
    "where s.run_id = h.target_run_id having count(*) > 0) cs "
    "where h.environment = (select environment from mart.environment_identity) "
    "and h.factor_id = %s and h.universe_id like %s "
    "order by h.advanced_at desc limit 1"
)


def governed_head(connection: Connection[Any], *, universe_prefix: str) -> GovernedHead | None:
    row = connection.execute(GOVERNED_HEAD_SQL, (GOVERNING_FACTOR, universe_prefix + "%")).fetchone()
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


def _reason_of(codes: Sequence[Any], *, not_applicable: bool = False) -> str:
    """The code that explains an empty cell: its first outcome code in the reason registry.

    An evidence code describes a value and never explains a gap, so it is skipped. In a cell that
    the served status marks not applicable, a code of the class `not_applicable` comes first. A code
    the registry does not list stays the reason, so the report counts it as a violation.
    """
    outcomes = [
        str(code) for code in codes if (entry := registration(str(code))) is None or entry.kind is ReasonKind.OUTCOME
    ]
    if not_applicable:
        for code in outcomes:
            entry = registration(code)
            if entry is not None and entry.reason_class is ReasonClass.NOT_APPLICABLE:
                return code
    return outcomes[0] if outcomes else UNRECORDED_REASON


def _classify_cell(subject_id: Any, status: Any, reason_codes: Any, value: Any) -> Cell:
    """The served status alone says that a measure does not apply (the producer sets it, the TOPT
    baseline counts it); a reason class only routes the gap (#1851)."""
    if status == "available" and value is not None:
        return Cell(str(subject_id), True)
    if status == "low_confidence" and value is not None:
        reason = _reason_of(list(reason_codes or []))
        return Cell(str(subject_id), True, "low_confidence" if reason == UNRECORDED_REASON else reason)
    if status in {"available", "low_confidence"} and value is None:
        return Cell(str(subject_id), False, "null_metric_value")
    not_applicable = status == "not_applicable"
    reason = _reason_of(list(reason_codes or []), not_applicable=not_applicable)
    return Cell(str(subject_id), False, reason, not_applicable=not_applicable)


def _query_standard_cells(connection: Connection[Any], sql: str, run_id: str) -> tuple[Cell, ...]:
    rows = connection.execute(sql, (run_id,)).fetchall()
    return tuple(_classify_cell(r[0], r[1], r[2], r[3] if len(r) > 3 else Decimal("1")) for r in rows)


def gppe_cells(connection: Connection[Any], run_id: str) -> tuple[Cell, ...]:
    rows = connection.execute(
        "select distinct on (mnv.issuer_id) mnv.issuer_id, mnv.availability_status, mnv.reason_codes, mnv.value "
        "from mart.metric_node_values mnv "
        "left join mart.capture_issuer_lineage cil using (run_id, issuer_id) "
        "where mnv.run_id = %(run_id)s "
        f"and {gppe_branch_predicate('mnv', 'cil', bank_param='%(bank_key)s', base_param='%(base_key)s')} "
        "order by mnv.issuer_id, (mnv.node_key = %(bank_key)s) desc",
        {"run_id": run_id, "bank_key": GPPE_BANKING_TCE_SPEC.root, "base_key": GPPE_V0_SPEC.root},
    ).fetchall()
    return tuple(_classify_cell(r[0], r[1], r[2], r[3] if len(r) > 3 else Decimal("1")) for r in rows)


def peg_cells(connection: Connection[Any], *, run_id: str) -> tuple[Cell, ...]:
    rows = connection.execute(
        "with chosen as (select binding.strategy_run_id from mart.strategy_run_capture_bindings binding "
        "where binding.capture_run_id = %s order by binding.bound_at desc, binding.strategy_run_id desc limit 1) "
        "select d.issuer_id, d.peg, d.availability_status, d.exclusion_reason, d.peg_reason_codes "
        "from mart.strategy_decisions d where d.strategy_run_id = (select strategy_run_id from chosen) order by d.issuer_id",
        (run_id,),
    ).fetchall()
    cells = []
    for subject_id, peg, availability_status, exclusion_reason, peg_reason_codes in rows:
        if peg is not None:
            cells.append(Cell(str(subject_id), True))
            continue
        # #1804: PEG is computed for an excluded decision too, so its own reason wins; the exclusion
        # explains the cell only when PEG gives none.
        reason = _reason_of(list(peg_reason_codes or []))
        if reason == UNRECORDED_REASON and availability_status == "excluded" and exclusion_reason:
            reason = _reason_of([f"excluded:{exclusion_reason}"])
        cells.append(Cell(str(subject_id), False, reason))
    return tuple(cells)


def fund_cells(connection: Connection[Any], run_id: str) -> tuple[Cell, ...]:
    return _query_standard_cells(
        connection,
        "select fund_id, availability_status, reason_codes, weighted_valuation_gap from mart.fund_virtual_company where run_id = %s order by fund_id",
        run_id,
    )


def expected_fund_ids(connection: Connection[Any], cutoff: datetime) -> tuple[str, ...]:
    rows = connection.execute(
        "select distinct fund_id from mart.fund_holdings_resolved where transaction_time <= %s order by fund_id",
        (cutoff,),
    ).fetchall()
    return tuple(str(r[0]) for r in rows if r and r[0])


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
    expected = tuple(dict.fromkeys(funds if requirement.scope is QuestionScope.FUND else issuers))
    columns = [column for column in requirement.columns if column.applies_to(universe_id)]
    col_keys = [_column_key(column) for column in columns]
    entry: dict[str, Any] = {
        "text": QUESTION_TEXT[requirement.question],
        "tracking_issue": requirement.tracking_issue,
        "scope": requirement.scope.value,
        "denominator": len(expected),
        "world_absence": 0,
        "pipeline_gap": len(expected) if not col_keys else 0,
        "actionable_denominator": len(expected),
        "actionable_coverage_pct": 0.0 if expected else 100.0,
        "column": col_keys[0] if col_keys else None,
        "columns": col_keys,
        "answered": 0,
        "unavailable": {},
        "missing": len(expected) if not col_keys else 0,
    }
    if not columns:
        return entry
    observed = [{cell.subject_id: cell for cell in cells_by_column.get(key, ())} for key in col_keys]
    unavailable: Counter[str] = Counter()
    by_class: Counter[str] = Counter()
    violations: Counter[str] = Counter()
    unregistered: Counter[str] = Counter()
    reasons: dict[str, str] = {}
    answered = 0
    applicable = 0
    not_applicable_count = 0
    for subject_id in expected:
        cells = [lookup[subject_id] for lookup in observed if subject_id in lookup]
        if cells and any(cell.answered for cell in cells):
            answered += 1
            applicable += 1
            continue
        if not cells:
            reason, not_applicable = NO_ROW, False
        else:
            reason, not_applicable = cells[0].reason or UNRECORDED_REASON, all(cell.not_applicable for cell in cells)
        reasons[subject_id] = reason
        registered = registration(reason)
        violation = (
            VIOLATION_NO_ROW
            if not cells
            else VIOLATION_NO_REASON
            if reason == UNRECORDED_REASON
            else VIOLATION_UNREGISTERED
            if registered is None or registered.reason_class is None
            else VIOLATION_STATUS_CLASS_CONFLICT
            if registered.reason_class is ReasonClass.NOT_APPLICABLE and not not_applicable
            else None
        )
        if violation is not None:
            violations[violation] += 1
            if violation == VIOLATION_UNREGISTERED:
                unregistered[reason] += 1
        elif registered is not None and registered.reason_class is not None:
            # Each empty cell is in one class or one violation, never both (#1851).
            by_class[ReasonClass.NOT_APPLICABLE.value if not_applicable else registered.reason_class.value] += 1
        if not_applicable:
            not_applicable_count += 1
            continue
        applicable += 1
        unavailable[reason] += 1
    entry["denominator"] = applicable
    entry["answered"] = answered
    world_absence = int(by_class.get(ReasonClass.WORLD_ABSENCE.value, 0))
    pipeline_gap = max(0, applicable - answered - world_absence)
    actionable_denom = max(0, applicable - world_absence)
    entry["world_absence"] = world_absence
    entry["pipeline_gap"] = pipeline_gap
    entry["actionable_denominator"] = actionable_denom
    entry["actionable_coverage_pct"] = (
        round((answered * 100.0) / actionable_denom, 1) if actionable_denom > 0 else (100.0 if applicable == 0 else 0.0)
    )
    entry["unavailable"] = _by_count(unavailable)
    if not_applicable_count:
        entry["not_applicable"] = not_applicable_count
    entry["by_class"] = _by_count(by_class)
    entry["violations"] = _by_count(violations)
    if unregistered:
        entry["unregistered_reasons"] = _by_count(unregistered)
    entry["reasons"] = dict(sorted(reasons.items()))
    return entry


def _by_count(counts: Counter[str]) -> dict[str, int]:
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def with_change(
    questions: dict[str, dict[str, Any]], before: Mapping[str, Mapping[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    """Add to each question what changed since `before`, the previous head classified under the same
    rules: the answered count, each class, each reason, and the subjects whose reason moved (#1851
    layer 2). The per-subject reasons are dropped from what is stored."""
    for question, entry in questions.items():
        now = entry.pop("reasons", {})
        if before is None:
            continue
        then_entry = before.get(question) or {}
        then = then_entry.get("reasons") or {}
        moved = sorted(
            (subject, then.get(subject), now.get(subject))
            for subject in set(now) | set(then)
            if then.get(subject) != now.get(subject)
        )
        entry["change"] = {
            "answered": int(entry.get("answered", 0)) - int(then_entry.get("answered", 0)),
            "by_class": _delta(entry.get("by_class") or {}, then_entry.get("by_class") or {}),
            "unavailable": _delta(entry.get("unavailable") or {}, then_entry.get("unavailable") or {}),
            "subjects_changed": len(moved),
            "examples": [
                {"subject": subject, "before": old, "after": new}
                for subject, old, new in moved[:MAX_CHANGED_SUBJECTS_LISTED]
            ],
        }
    return questions


def _delta(now: Mapping[str, int], then: Mapping[str, int]) -> dict[str, int]:
    keys = set(now) | set(then)
    return {
        key: int(now.get(key, 0)) - int(then.get(key, 0)) for key in sorted(keys) if now.get(key, 0) != then.get(key, 0)
    }


def compile_report(connection: Connection[Any], *, universe: str, executed_at: datetime) -> dict[str, Any] | None:
    key = key_of_lane(universe)
    head = governed_head_of(connection, key)
    if isinstance(head, NoHead):
        return None
    issuers, questions = _questions(connection, key, head)
    previous = previous_head(connection, universe, head.run_id)
    previous_issuers, before = (0, None) if previous is None else _questions(connection, key, previous)
    if not previous_issuers:
        before = None  # no previous head, or its rows are gone: no change rather than a false one
    return {
        "universe": universe,
        "universe_id": head.universe_id,
        "run_id": head.run_id,
        "cutoff": head.cutoff.astimezone(UTC).isoformat(),
        "environment": declared_environment(connection),
        "requirements_sha256": QUESTION_REQUIREMENTS_SHA256,
        "reason_registry_sha256": REGISTRY_SHA256,
        "compiler_version": REPORT_COMPILER_VERSION,
        "generated_at": executed_at.astimezone(UTC).isoformat(),
        "denominator": issuers,
        "previous_run_id": None if before is None or previous is None else previous.run_id,
        "questions": with_change(questions, before),
    }


def _frozen_member_issuers(connection: Connection[Any], run_id: str) -> list[str]:
    rows = connection.execute(
        "select distinct m.issuer_id from staging.core_snapshot_members m "
        "join staging.core_snapshots s on s.snapshot_id = m.snapshot_id "
        "where s.run_id = %s order by m.issuer_id",
        (run_id,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _questions(connection: Connection[Any], key: str, head: GovernedHead) -> tuple[int, dict[str, dict[str, Any]]]:
    gppe = gppe_cells(connection, head.run_id)
    frozen_issuers = _frozen_member_issuers(connection, head.run_id)
    issuers = list(dict.fromkeys([*frozen_issuers, *(cell.subject_id for cell in gppe)]))
    consolidates_fund = "fund_consolidation" in record(key).roles
    funds_observed = fund_cells(connection, head.run_id) if consolidates_fund else ()
    fund_subjects = tuple(
        dict.fromkeys(
            [
                *(expected_fund_ids(connection, head.cutoff) if consolidates_fund else ()),
                *(c.subject_id for c in funds_observed),
            ]
        )
    )
    cells_by_column = {
        "mart.metric_node_values.value": gppe,
        "mart.strategy_decisions.peg": peg_cells(connection, run_id=head.run_id)
        if "strategy" in record(key).roles
        else (),
        "mart.issuer_supply_chain_exposure.exposure_score": supply_chain_cells(connection, head.run_id),
        "mart.issuer_analyst_ratings.consensus_rating": analyst_rating_cells(connection, head.run_id),
        "mart.fund_virtual_company.weighted_valuation_gap": funds_observed,
        "mart.issuer_theme_purity.theme_share": theme_purity_cells(connection, head.run_id),
    }
    return len(issuers), {
        req.question.value: classify_question(
            req,
            universe_id=head.universe_id,
            issuers=issuers,
            funds=fund_subjects,
            cells_by_column=cells_by_column,
        )
        for req in QUESTION_REQUIREMENTS
    }


def previous_head(connection: Connection[Any], universe: str, run_id: str) -> GovernedHead | None:
    """The head of the newest stored report of the universe for another run. The universe id of a
    list universe changes with its membership, so the lane name finds it (#1851 review)."""
    row = connection.execute(
        "select universe_id, run_id, cutoff from mart.question_coverage_report "
        "where universe_id like %s and payload ->> 'universe' = %s and run_id <> %s order by created_at desc limit 1",
        (f"{record(key_of_lane(universe)).id_prefix}%", universe, run_id),
    ).fetchone()
    return None if row is None else GovernedHead(universe_id=str(row[0]), run_id=str(row[1]), cutoff=row[2])


def persist(connection: Connection[Any], report: Mapping[str, Any]) -> str:
    return persist_run_report(
        connection,
        report,
        table="mart.question_coverage_report",
        report_id_prefix="question-coverage-report",
        columns=("universe_id", "run_id", "cutoff", "requirements_sha256"),
    )


def _newest_run_in(connection: Connection[Any], table: str, universe_id: str) -> str | None:
    row = connection.execute(
        f"select run_id from {table} where universe_id = %s order by created_at desc limit 1",  # noqa: S608
        (universe_id,),
    ).fetchone()
    return str(row[0]) if row else None


def stored_report_run(connection: Connection[Any], universe_id: str) -> str | None:
    return _newest_run_in(connection, "mart.question_coverage_report", universe_id)


def stored_report_rules_current(connection: Connection[Any], universe_id: str) -> bool:
    """Whether the newest stored report was compiled under this reason registry and compiler version.
    A report of other rules is not current, so the head reports run again (#1851, #1747)."""
    row = connection.execute(
        "select payload ->> 'reason_registry_sha256', payload ->> 'compiler_version' "
        "from mart.question_coverage_report where universe_id = %s order by created_at desc limit 1",
        (universe_id,),
    ).fetchone()
    return row is not None and row[0] == REGISTRY_SHA256 and row[1] == REPORT_COMPILER_VERSION


def stored_confidence_report_run(connection: Connection[Any], universe_id: str) -> str | None:
    return _newest_run_in(connection, "mart.datahub_confidence_report", universe_id)


def reports_current_for_head(connection: Connection[Any], head: GovernedHead) -> bool:
    if stored_report_run(connection, head.universe_id) != head.run_id:
        return False
    if not stored_report_rules_current(connection, head.universe_id):
        return False
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
        exceeded = int(unavailable_map.get("context_window_exceeded", 0))
        exceeded_note = f" ({exceeded} >1M context_window_exceeded)" if exceeded > 0 else ""
        act_denom = int(entry.get("actionable_denominator", entry["denominator"]))
        act_pct = entry.get("actionable_coverage_pct")
        gaps = entry.get("pipeline_gap")
        pct_note = f" ({act_pct:.1f}% actionable, gaps={gaps})" if act_pct is not None and gaps is not None else ""
        parts.append(
            f"{question}: {entry['answered']}/{act_denom} answered{pct_note}, "
            f"{sum(unavailable_map.values())} unavailable{exceeded_note}, {entry['missing']} missing"
        )
    return f"{report['universe_id']} @ {report['cutoff']}: " + "; ".join(parts)
