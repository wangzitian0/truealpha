"""The moomoo quota gate (init.md Section 1 rule 6: every moomoo call
must go through the gate, no module decides for itself).

Per owner directive on #1411 (K2) and #1520 (Step 2):
- There is no monthly call budget.
- OpenD calls are admitted against the bound CapacityGate by their seat's share.
- Distinct-stock K-line quota:
  `request_history_kline` / `get_history_kline` tracks distinct stocks (`request_uri`
  codes) over the past 7 days and raises `BudgetExceededError` if a new stock would
  exceed the environment's share of the stored `get_history_kl_quota` reading.

This module only reads `staging.api_call_ledger`. The one writer is `gateway.record_call`
(#1521): `sources.moomoo._call` records every OpenD call through it, so a moomoo row has the
same shape, the same failure handling and the same table as every other source's row.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from truealpha_contracts.environment_contract import load_environment_contract

from data_engine.sources import gateway
from data_engine.sources.gateway import _pg_execute

logger = logging.getLogger(__name__)

#: Endpoints that consume the 7-day distinct-stock candlestick quota.
KLINE_ENDPOINTS = frozenset({"request_history_kline", "get_history_kline"})

_stored_history_kl_quota: int | None = None


class BudgetExceededError(Exception):
    pass


# --- stored get_history_kl_quota reading (#1520) ---


def store_history_kl_quota(reading: int) -> None:
    """Store the total historical-candlestick stock quota reading (#1520)."""
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
    """Return the latest stored `get_history_kl_quota` reading, or None if no reading is stored."""
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
    """Reset the in-memory stored quota reading (test double)."""
    global _stored_history_kl_quota
    _stored_history_kl_quota = None


def distinct_stock_quota(*, environment: str | None = None) -> int | None:
    """The allowed number of distinct stocks for this environment in 7 days (#1520).

    Derived as `stored_reading * share.percent // 100` from the environment contract's
    moomoo account share.
    """
    reading = stored_history_kl_quota()
    if reading is None:
        return None
    contract = load_environment_contract()
    account = contract.account_of_seat("moomoo")
    env = environment if environment is not None else gateway.capacity_environment()
    share = contract.account_share(account, env)
    return reading * share.percent // 100


# --- reads of the ledger the one writer fills ---

_MONTH_START_UTC = "date_trunc('month', now() at time zone 'utc') at time zone 'utc'"
_DAY_START_UTC = "date_trunc('day', now() at time zone 'utc') at time zone 'utc'"


def calls_this_month() -> int:
    """Return total moomoo calls recorded since the start of this UTC month."""
    row = _pg_execute(
        f"select count(*) from staging.api_call_ledger where source = 'moomoo' and called_at >= {_MONTH_START_UTC}"
    ).fetchone()
    return row[0]


def calls_today() -> int:
    """Return total moomoo calls recorded since 00:00 UTC today."""
    row = _pg_execute(
        f"select count(*) from staging.api_call_ledger where source = 'moomoo' and called_at >= {_DAY_START_UTC}"
    ).fetchone()
    return row[0]


def normalize_stock_uri(code_or_uri: str | None) -> str | None:
    if not code_or_uri:
        return None
    text = code_or_uri.strip()
    if not text:
        return None
    return text if text.startswith("moomoo://") else f"moomoo://{text}"


def kline_distinct_stocks_7d() -> set[str]:
    """Return the set of distinct ``moomoo://<code>`` URIs requested on K-line endpoints
    within the rolling 7-day window (#1411 K2, #1520)."""
    writer = getattr(gateway, "_writer", None)
    if writer is not gateway.postgres_writer and hasattr(writer, "__iter__"):
        cutoff = datetime.now(UTC) - timedelta(days=7)
        return {
            uri
            for row in gateway._writer
            if row.source == "moomoo"
            and row.endpoint in KLINE_ENDPOINTS
            and row.request_uri is not None
            and row.called_at >= cutoff
            and (uri := normalize_stock_uri(row.request_uri)) is not None
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
    """Enforce the 7-day rolling distinct-stock K-line quota (#1411 K2, #1520)."""
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
