"""Strategy backtest data boundary over captured staging inputs (#395)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, NamedTuple

from factors.composite.strategy_evaluator import IssuerInput, LookAheadError
from factors.measure import measure_for_input_key
from psycopg import Connection
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.fiscal_period import parse_annual

from data_engine.datahub._helpers import parse_utc


def _period_end_key(tag: str) -> tuple[date, str]:
    try:
        if (parsed := parse_annual(tag)) is not None:
            return parsed.end, tag
    except ValueError:
        pass
    parts = tag.split(":")
    if len(parts) >= 4:
        try:
            return date.fromisoformat(parts[-1]), tag
        except ValueError:
            pass
    if ":" not in tag and tag.startswith("FY") and len(tag) >= 6 and tag[2:6].isdigit():
        try:
            return date(int(tag[2:6]), 12, 31), tag
        except ValueError:
            pass
    return date.min, tag


InputRow = tuple[str, str, Any, Any, str | None, datetime]


class _Observed(NamedTuple):
    value: Decimal
    confidence: Decimal
    knowable_at: datetime


class MarketValueVintage(NamedTuple):
    issuer_id: str
    shares_knowable_at: datetime
    close_knowable_at: datetime


class StrategyBacktestGateway:
    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection

    def rows_at(self, cutoff_at: str | datetime) -> list[InputRow]:
        return self._connection.execute(
            "select distinct on (issuer_id, input_key, fiscal_period) "
            "issuer_id, input_key, value, confidence, fiscal_period, knowable_at "
            "from staging.strategy_backtest_inputs where cutoff_at = %s "
            "order by issuer_id, input_key, fiscal_period, knowable_at desc, recorded_at desc",
            (cutoff_at,),
        ).fetchall()

    def market_value_vintages_at(self, cutoff_at: str | datetime) -> list[MarketValueVintage]:
        rows = self._connection.execute(
            "select issuer_id, "
            "max(knowable_at) filter (where input_key = 'shares_outstanding'), "
            "max(knowable_at) filter (where input_key = 'last_close') "
            "from staging.strategy_backtest_inputs "
            "where cutoff_at = %s and fiscal_period is null and input_key in ('shares_outstanding', 'last_close') "
            "group by issuer_id having count(distinct input_key) = 2 order by issuer_id",
            (cutoff_at,),
        ).fetchall()
        return [MarketValueVintage(*row) for row in rows]

    def issuer_inputs(self, cutoff_at: str | datetime, *, enforce_pit: bool = False) -> list[IssuerInput]:
        cutoff_dt = parse_utc(cutoff_at) if enforce_pit else None
        return issuer_inputs_from_rows(self.rows_at(cutoff_at), cutoff_at=cutoff_dt)

    def snapshot_id(self, cutoff_at: str | datetime) -> str:
        return snapshot_id_from_rows(self.rows_at(cutoff_at))

    def run_snapshot_id(self, cutoff_ats: list[str]) -> str:
        return combine_snapshot_ids([self.snapshot_id(cutoff) for cutoff in sorted(cutoff_ats)])


def combine_snapshot_ids(snapshot_ids: list[str]) -> str:
    return f"strategy-snapshot:{canonical_sha256(snapshot_ids)}"


def _measure_pair(key: str, seen: _Observed, subject: str, *, period: str | None = None) -> tuple[Any, Decimal]:
    kwargs: dict[str, Any] = {"subject": subject, "knowable_at": seen.knowable_at}
    if period is not None:
        kwargs["period"] = period
    return measure_for_input_key(key, seen.value, **kwargs), seen.confidence


def issuer_inputs_from_rows(rows: Sequence[InputRow], *, cutoff_at: datetime | None = None) -> list[IssuerInput]:
    scalars: dict[str, dict[str, _Observed]] = {}
    periods: dict[str, dict[str, dict[str, _Observed]]] = {}
    for issuer_id, input_key, value, confidence, fiscal_period, knowable_at in rows:
        if cutoff_at is not None and knowable_at > cutoff_at:
            raise LookAheadError(
                f"{issuer_id}: input {input_key!r} has knowable_at {knowable_at.isoformat()} > cutoff {cutoff_at.isoformat()}"
            )
        observed = _Observed(Decimal(str(value)), Decimal(str(confidence)), knowable_at)
        if fiscal_period is None:
            scalars.setdefault(issuer_id, {})[input_key] = observed
            continue
        series = periods.setdefault(issuer_id, {}).setdefault(input_key, {})
        series[fiscal_period] = observed
        scalars.setdefault(issuer_id, {})[input_key] = series[max(series, key=_period_end_key)]
    return [
        IssuerInput(
            issuer_id=issuer_id,
            records={key: _measure_pair(key, seen, issuer_id) for key, seen in answers.items()},
            knowable_at={key: seen.knowable_at for key, seen in answers.items()},
            periodic_records={
                key: {period: _measure_pair(key, seen, issuer_id, period=period) for period, seen in by_period.items()}
                for key, by_period in periods.get(issuer_id, {}).items()
            },
        )
        for issuer_id, answers in sorted(scalars.items())
    ]


def snapshot_id_from_rows(rows: Sequence[InputRow]) -> str:
    payload = sorted(
        (issuer_id, input_key, str(value), str(confidence), fiscal_period or "")
        for issuer_id, input_key, value, confidence, fiscal_period, _knowable_at in rows
    )
    return f"strategy-snapshot:{canonical_sha256(payload)}"
