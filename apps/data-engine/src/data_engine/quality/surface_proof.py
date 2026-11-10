# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; from collections.abc import Mapping, Sequence; from dataclasses import dataclass, replace; from datetime import datetime, timedelta; from typing import Any; from psycopg import Connection; from truealpha_contracts.universes import key_of_lane, lane_name, record, served_keys, universe_with_role; from data_engine.datahub.question_coverage import ( GOVERNING_FACTOR, GovernedHead, compile_report, declared_environment, governed_head, stored_report_run, ); from data_engine.lanes.capture import is_universe_scheduled; from data_engine.sources import llm; _NOT_SCHEDULED, _NOT_RUN_HERE = "not_scheduled", "not-run-in-this-environment"; _THEMES_SURFACE, _THEMES_READER = "/research/themes", "theme-purity.ts LATEST_RUN_SQL"; _STRATEGY_HEAD_SQL = "select target_run_id, strategy_run_id from mart.governed_strategy_run"; _THEMES_HEAD_SQL = "select p.run_id, max(p.cutoff) as cutoff from mart.issuer_theme_purity p where p.partition_id is not null and not exists ( select 1 from mart.served_head h where h.head_run_id = p.run_id and h.availability = 'unavailable' ) group by run_id order by max(cutoff) desc limit 1"; _THEME_ROWS_SQL = ( "select exists (select 1 from mart.issuer_theme_purity where run_id = %s and partition_id is not null)" ); _THEME_UNIVERSE_SQL = "select exists ( select 1 from mart.current_pointer p join mart.issuer_theme_purity t on t.run_id = p.target_run_id and t.partition_id is not null where p.environment = %s and p.factor_id = %s and p.universe_id like %s )"; _HOLDINGS_HEAD_SQL = "select run_id, freshness, availability, staleness_reason from mart.served_head where factor_id = 'gross_profit_per_employee' and universe_id like %s order by advanced_at desc limit 1"; _HOLDINGS_ROWS_SQL = "select count(*) from mart.fund_virtual_company where run_id = %s"; _COVERAGE_HEAD_SQL = "select distinct on (universe_id) universe_id, run_id, payload from mart.question_coverage_report r where universe_id in (select universe_id from mart.served_head) order by universe_id, exists ( select 1 from mart.served_head h where h.universe_id = r.universe_id and coalesce(h.run_id, h.head_run_id) = r.run_id ) desc, created_at desc"; _SEGMENT_PARTITIONS_SQL = ( "select count(distinct partition_id) from staging.issuer_segment_revenue_facts where knowable_at <= %s" ); _POINTER_RECORDED_SQL = "select max(created_at) from mart.current_pointer where environment = %s and factor_id = %s and universe_id = %s and target_run_id = %s"
@dataclass(frozen=True)
class SurfaceVerdict:
    surface: str; reader: str; expected_run: str | None; served_run: str | None; detail: str = ""; stale: bool = False; universe: str | None = None; in_progress: str = ""
    @property
    def ok(self) -> bool: return self.expected_run is not None and self.served_run == self.expected_run and not self.stale
    @property
    def state(self) -> str: return "MATCH" if self.ok else ("IN-PROGRESS" if self.in_progress else "MISMATCH")
    @property
    def mismatched(self) -> bool: return self.state == "MISMATCH"
    @property
    def line(self) -> str: served, expected = self.served_run or "nothing", self.expected_run or "no governed head"; notes = [n for n in (self.detail, self.in_progress) if n]; return f"{self.state} {self.surface}: serves {served[:24]} vs head {expected[:24]}" + ( f" — {'; '.join(notes)}" if notes else "" )
def _heads(connection: Connection[Any]) -> dict[str, GovernedHead | None]: return {lane_name(key): governed_head(connection, universe_prefix=record(key).id_prefix) for key in served_keys()}
def fresh_heads_without_reports(connection: Connection[Any], *, now: datetime, grace: timedelta) -> dict[str, str]:
    settling: dict[str, str] = {}; environment = declared_environment(connection)
    for universe, head in _heads(connection).items():
        if head is None or stored_report_run(connection, head.universe_id) == head.run_id: continue
        row = connection.execute( _POINTER_RECORDED_SQL, (environment, GOVERNING_FACTOR, head.universe_id, head.run_id) ).fetchone()
        if (recorded := row[0] if row else None) is not None and now - recorded < grace: minutes = max(0, int((now - recorded).total_seconds() // 60)); settling[universe] = f"head {head.run_id[:24]} recorded {minutes} min ago; its reports are not written yet"
    return settling
def prove( connection: Connection[Any], *, executed_at: datetime, settling: Mapping[str, str] | None = None ) -> tuple[SurfaceVerdict, ...]:
    environment, heads = declared_environment(connection), _heads(connection); strategy_key, fund_key = universe_with_role("strategy"), universe_with_role("fund_consolidation"); topt, qqq = lane_name(strategy_key), lane_name(fund_key); row = connection.execute(_STRATEGY_HEAD_SQL).fetchone(); verdicts: list[SurfaceVerdict] = [ SurfaceVerdict( "/research/rankings, /strategy, /compare, /trace, /coverage", "mart.governed_strategy_run", _run(heads.get(topt)), str(row[0]) if row else None, f"strategy run {row[1]}" if row else "the view is empty: no strategy run at the head's cutoff", universe=topt, ) ]; row = connection.execute(_THEMES_HEAD_SQL).fetchone()
    if row is None and not llm.is_configured(): verdicts.append( SurfaceVerdict( _THEMES_SURFACE, _THEMES_READER, _NOT_RUN_HERE, _NOT_RUN_HERE, "no model provider seated in this environment; the theme lane is off", universe=topt, ) )
    else: verdicts.append(themes_verdict(connection, heads, str(row[0]) if row else None))
    row = connection.execute(_HOLDINGS_HEAD_SQL, (f"{record(fund_key).id_prefix}%",)).fetchone(); served = str(row[0]) if row else None; counted = connection.execute(_HOLDINGS_ROWS_SQL, (served,)).fetchone() if served else None; funds = int(counted[0]) if counted else 0
    if heads.get(qqq) is None and served is None: verdicts.append( SurfaceVerdict( "/research/holdings", "fund-valuation.ts QQQ_POINTER_HEAD_SQL", _NOT_RUN_HERE, _NOT_RUN_HERE, f"no {fund_key} head in this environment; the page renders filed weights unvalued", universe=qqq, ) )
    else: verdicts.append( SurfaceVerdict( "/research/holdings", "fund-valuation.ts QQQ_POINTER_HEAD_SQL", _run(heads.get(qqq)), served, f"{funds} consolidated fund row(s)", stale=served is not None and funds == 0, universe=qqq, ) )
    stored = {str(u): (str(r), p) for u, r, p in connection.execute(_COVERAGE_HEAD_SQL).fetchall()}
    for universe, head in heads.items():
        prefix = record(key_of_lane(universe)).id_prefix; surface, reader = f"/admin/datahub coverage [{universe}]", "datahub-stats.ts QUESTION_COVERAGE_SQL"
        if not is_universe_scheduled(key_of_lane(universe), environment) and universe not in (settling or {}): verdicts.append( SurfaceVerdict( surface, reader, _NOT_SCHEDULED, _NOT_SCHEDULED, "not_scheduled in this environment", universe=universe, ) ); continue
        match = ( stored.get(head.universe_id) if head is not None else next((e for u, e in stored.items() if u.startswith(prefix)), None) )
        if head is None and match is None: verdicts.append( SurfaceVerdict( surface, reader, _NOT_RUN_HERE, _NOT_RUN_HERE, "no head and no report for this universe in this environment", universe=universe, ) ); continue
        drift = _drift( match[1] if match else None, compile_report(connection, universe=universe, executed_at=executed_at) ); verdicts.append( SurfaceVerdict( surface, reader, _run(head), match[0] if match else None, drift or "recomputed report agrees", stale=bool(drift), universe=universe, ) )
    pending = settling or {}; return tuple(_settle(v, pending) for v in verdicts)
def _settle(verdict: SurfaceVerdict, pending: Mapping[str, str]) -> SurfaceVerdict: why = pending.get(verdict.universe) if verdict.universe is not None else None; return replace(verdict, in_progress=why) if why and not verdict.ok else verdict
def _run(head: GovernedHead | None) -> str | None: return head.run_id if head else None
def _exists(connection: Connection[Any], sql: str, params: tuple[Any, ...]) -> bool: row = connection.execute(sql, params).fetchone(); return bool(row and row[0])
def themes_verdict( connection: Connection[Any], heads: Mapping[str, GovernedHead | None], served: str | None ) -> SurfaceVerdict:
    environment = declared_environment(connection); present = {u: h for u, h in heads.items() if h is not None}; covered = { u: h for u, h in present.items() if _exists( connection, _THEME_UNIVERSE_SQL, (environment, GOVERNING_FACTOR, record(key_of_lane(u)).id_prefix + "%") ) }; candidates = covered or present
    if not candidates: return SurfaceVerdict( _THEMES_SURFACE, _THEMES_READER, None, served, "" if served else _no_theme_rows(connection, None), universe=lane_name(universe_with_role("strategy")), )
    newest = max(h.cutoff for h in candidates.values()); tied = sorted(u for u, h in candidates.items() if h.cutoff == newest); universe = next((name for name in tied if candidates[name].run_id == served), tied[0]); head = candidates[universe]
    if served is None: detail = _no_theme_rows(connection, head)
    elif served == head.run_id: detail = f"the {universe} head, the newest of a universe with theme rows"
    else:
        notes: list[str] = []
        if not covered: notes.append("no run a governed pointer named has theme rows")
        elif not _exists(connection, _THEME_ROWS_SQL, (head.run_id,)): notes.append(f"the {universe} head, the newest of a universe with theme rows, has none")
        owner = next((name for name, other in present.items() if other.run_id == served), None); notes.append( f"the served run is the {owner} head" if owner else "the served run is no universe's governed head" ); detail = "; ".join(notes)
    return SurfaceVerdict(_THEMES_SURFACE, _THEMES_READER, head.run_id, served, detail, universe=universe)
def _no_theme_rows(connection: Connection[Any], head: GovernedHead | None) -> str:
    if head is None: return "no theme purity rows at all"
    row = connection.execute(_SEGMENT_PARTITIONS_SQL, (head.cutoff,)).fetchone()
    if (partitions := int(row[0]) if row else 0) == 0: return "no theme purity rows at all: no segment partition is knowable at the head's cutoff (the segment_revenue backfill has landed nothing here)"
    return f"no theme purity rows at all, though {partitions} segment partition(s) are knowable at the head's cutoff"
_DRIFT_FIELDS = ("answered", "unavailable", "missing")
def _drift_value(entry: Mapping[str, Any], field: str) -> Any: return entry.get(field)
def _drift(stored: Any, fresh: dict[str, Any] | None) -> str:
    if stored is None: return "no stored report"
    if fresh is None: return "no governed head to recompute against"
    stored_questions = (stored or {}).get("questions", {}); changed = []
    for question, entry in fresh["questions"].items():
        if (before := stored_questions.get(question)) is None: changed.append(f"{question}: absent from the stored report"); continue
        moved = [f for f in _DRIFT_FIELDS if _drift_value(before, f) != _drift_value(entry, f)]
        if "answered" in moved: changed.append(f"{question}: stored {before.get('answered')} answered, tables say {entry['answered']}")
        elif moved: changed.append( f"{question}: {', '.join(f'{f} {_drift_value(before, f)} -> {_drift_value(entry, f)}' for f in moved)}" )
    return "; ".join(changed)
def summary_lines(verdicts: Sequence[SurfaceVerdict]) -> list[str]: return [*(v.line for v in verdicts), f"report surface proof: {verdict_counts(verdicts)}"]
def verdict_counts(verdicts: Sequence[SurfaceVerdict]) -> str: matched = sum(1 for v in verdicts if v.ok); settling = sum(1 for v in verdicts if v.state == "IN-PROGRESS"); failed = sum(1 for v in verdicts if v.mismatched); return ( f"{matched}/{len(verdicts)} surfaces serve the governed head" + (f"; {settling} in progress" if settling else "") + (f"; {failed} do not" if failed else "") )
