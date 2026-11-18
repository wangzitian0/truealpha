"""Materialized-view refresh helpers for converged mart projections (#1730 Slice 5)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg import sql
from psycopg.pq import TransactionStatus

MART_ENTITY_IDENTITY = "mart.entity_identity"
MART_FUND_HOLDINGS = "mart.fund_holdings"
MART_FUND_HOLDINGS_RESOLVED = "mart.fund_holdings_resolved"
MART_FUND_HOLDINGS_COVERAGE = "mart.fund_holdings_coverage"
MART_FUND_HOLDINGS_VALUATION = "mart.fund_holdings_valuation"

MART_MV_REFRESH_ORDER: tuple[str, ...] = (
    MART_ENTITY_IDENTITY,
    MART_FUND_HOLDINGS,
    MART_FUND_HOLDINGS_RESOLVED,
    MART_FUND_HOLDINGS_COVERAGE,
    MART_FUND_HOLDINGS_VALUATION,
)
MART_FUND_HOLDINGS_MV_ORDER: tuple[str, ...] = (
    MART_FUND_HOLDINGS,
    MART_FUND_HOLDINGS_RESOLVED,
    MART_FUND_HOLDINGS_COVERAGE,
    MART_FUND_HOLDINGS_VALUATION,
)

_ALLOWED_MVS = frozenset(MART_MV_REFRESH_ORDER)


def _materialized_views_present(connection: Any, targets: tuple[tuple[str, str], ...]) -> set[tuple[str, str]]:
    rows = connection.execute(
        """
        select n.nspname, c.relname
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where c.relkind = 'm'
          and n.nspname = any(%s)
          and c.relname = any(%s)
        """,
        (list({s for s, _ in targets}), list({r for _, r in targets})),
    ).fetchall()
    return {(str(schema), str(rel)) for schema, rel in rows}


def refresh_mart_materialized_views(
    connection: Any,
    views: Sequence[str] = MART_MV_REFRESH_ORDER,
    *,
    concurrently: bool = True,
    commit_before: bool = False,
) -> tuple[str, ...]:
    """Refresh materialized mart views in dependency order.

    When inside an active SQL transaction (`TransactionStatus.INTRANS`), PostgreSQL disallows
    `CONCURRENTLY`, so we refresh in-place inside the caller's transaction so uncommitted test or
    staged writes become visible immediately. Outside a transaction (or with `commit_before=True`),
    `REFRESH MATERIALIZED VIEW CONCURRENTLY` runs in autocommit so `mart_readonly` readers are never blocked.
    """
    info = getattr(connection, "info", None)
    if info is None or not views:
        return ()

    targets: list[tuple[str, str]] = []
    for name in views:
        if name not in _ALLOWED_MVS:
            raise ValueError(f"unsupported mart materialized view: {name}")
        schema, rel = name.split(".", 1)
        targets.append((schema, rel))

    if commit_before and info.transaction_status == TransactionStatus.INTRANS:
        connection.commit()

    use_concurrent = concurrently and info.transaction_status != TransactionStatus.INTRANS
    refreshed: list[str] = []
    if use_concurrent:
        prev_autocommit = bool(connection.autocommit)
        connection.autocommit = True
        try:
            present = _materialized_views_present(connection, tuple(targets))
            active = [(schema, rel) for schema, rel in targets if (schema, rel) in present]
            for schema, rel in active:
                connection.execute(
                    sql.SQL("refresh materialized view concurrently {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(rel)
                    )
                )
                refreshed.append(f"{schema}.{rel}")
        finally:
            connection.autocommit = prev_autocommit
    else:
        present = _materialized_views_present(connection, tuple(targets))
        active = [(schema, rel) for schema, rel in targets if (schema, rel) in present]
        for schema, rel in active:
            connection.execute(
                sql.SQL("refresh materialized view {}.{}").format(sql.Identifier(schema), sql.Identifier(rel))
            )
            refreshed.append(f"{schema}.{rel}")
    return tuple(refreshed)
