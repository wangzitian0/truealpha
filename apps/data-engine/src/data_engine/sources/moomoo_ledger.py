"""Moomoo 7-day rolling distinct-stock K-line quota gate and ledger readers (#1411, #1520)."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from truealpha_contracts.environment_contract import load_environment_contract

from data_engine.sources import gateway
from data_engine.sources.gateway import _pg_execute

logger = logging.getLogger(__name__)

KLINE_ENDPOINTS = frozenset({"request_history_kline", "get_history_kline"})
_stored_history_kl_quota: int | None = None


class BudgetExceededError(Exception):
    pass


def store_history_kl_quota(reading: int) -> None:
    global _stored_history_kl_quota
    _stored_history_kl_quota = reading
    if getattr(gateway, "_writer", None) is gateway.postgres_writer:
        try:
            _pg_execute(
                """
                insert into staging.ingestion_health_log (source, metric, value, note)
                values ('moomoo', 'get_history_kl_quota', %s, 'stored reading')
                """,
                (reading,),
            )
        except Exception:
            pass


def stored_history_kl_quota() -> int | None:
    if _stored_history_kl_quota is not None:
        return _stored_history_kl_quota
    if getattr(gateway, "_writer", None) is gateway.postgres_writer:
        try:
            row = _pg_execute(
                """
                select value from staging.ingestion_health_log
                where source = 'moomoo' and metric = 'get_history_kl_quota'
                order by logged_at desc, id desc limit 1
                """
            ).fetchone()
            if row and row[0] is not None:
                return int(row[0])
        except Exception:
            pass
    return None


def reset_stored_history_kl_quota() -> None:
    global _stored_history_kl_quota
    _stored_history_kl_quota = None


def distinct_stock_quota(*, environment: str | None = None) -> int | None:
    reading = stored_history_kl_quota()
    if reading is None:
        return None
    contract = load_environment_contract()
    account = contract.account_of_seat("moomoo")
    env = environment if environment is not None else gateway.capacity_environment()
    share = contract.account_share(account, env)
    return reading * share.percent // 100


_MONTH_START_UTC = "date_trunc('month', now() at time zone 'utc') at time zone 'utc'"
_DAY_START_UTC = "date_trunc('day', now() at time zone 'utc') at time zone 'utc'"


def _calls_since_sql(since_expr: str) -> int:
    row = _pg_execute(
        f"select count(*) from staging.api_call_ledger where source = 'moomoo' and called_at >= {since_expr}"
    ).fetchone()
    return row[0]


def calls_this_month() -> int:
    return _calls_since_sql(_MONTH_START_UTC)


def calls_today() -> int:
    return _calls_since_sql(_DAY_START_UTC)


def normalize_stock_uri(code_or_uri: str | None) -> str | None:
    if not code_or_uri:
        return None
    text = code_or_uri.strip()
    if not text:
        return None
    return text if text.startswith("moomoo://") else f"moomoo://{text}"


def kline_distinct_stocks_7d() -> set[str]:
    writer = getattr(gateway, "_writer", None)
    if writer is not gateway.postgres_writer and isinstance(writer, Iterable):
        cutoff = datetime.now(UTC) - timedelta(days=7)
        return {
            uri
            for row in writer
            if getattr(row, "source", None) == "moomoo"
            and getattr(row, "endpoint", None) in KLINE_ENDPOINTS
            and getattr(row, "request_uri", None) is not None
            and getattr(row, "called_at", cutoff) >= cutoff
            and (uri := normalize_stock_uri(getattr(row, "request_uri", None))) is not None
        }
    rows = _pg_execute(
        """
        select distinct request_uri
        from staging.api_call_ledger
        where source = 'moomoo'
          and endpoint in ('request_history_kline', 'get_history_kline')
          and request_uri is not null
          and called_at >= now() - interval '7 days'
        """
    ).fetchall()
    return {uri for (raw,) in rows if (uri := normalize_stock_uri(raw)) is not None}


def gate(endpoint: str, caller: str, *, request_uri: str | None = None) -> None:
    if endpoint in KLINE_ENDPOINTS:
        quota = distinct_stock_quota()
        if quota is not None:
            stocks = kline_distinct_stocks_7d()
            target_uri = normalize_stock_uri(request_uri)
            if (target_uri is None or target_uri not in stocks) and len(stocks) >= quota:
                raise BudgetExceededError(
                    f"moomoo 7d K-line distinct-stock quota exhausted: {len(stocks)}/{quota} "
                    f"stocks used in the last 7 days (blocked call: {caller} -> {endpoint} {target_uri or ''})"
                )
