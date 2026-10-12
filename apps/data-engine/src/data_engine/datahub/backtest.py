"""Datahub backtest executor and market data loaders.

This module loads market prices and universe masks from staging tables.
It compiles factor panels and computes target weights.
It simulates portfolio performance with the backtest engine and writes results to mart tables.
"""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

import pandas as pd
import polars as pl
import psycopg
from factors.backtest.adapter import compile_factor_panel, compute_topk_dropout_weights, pivot_to_vbt_matrices
from factors.backtest.engine import BacktestEngineConfig, BacktestResult, NumpySimulationEngine
from factors.backtest.storage import persist_backtest_result
from factors.expressions.dsl import col

from data_engine.datahub.market_prices import SPLIT_ADJUSTED

if TYPE_CHECKING:
    from collections.abc import Sequence


def _load_prices(connection: psycopg.Connection, table: str, symbols: Sequence[str], *, adjust: str) -> pl.DataFrame:
    """Load price rows from a staging market price table for given symbols and adjustment mode.

    The query resolves multiple vintages to the latest vintage by sorting on recorded_at descending.
    """
    query = f"""
        select distinct on (symbol, trading_date) symbol, trading_date as date, close
        from {table}
        where symbol = any(%s) and adjust = %s
        order by symbol, trading_date, recorded_at desc
    """  # noqa: S608 - table is one of two fixed literals
    with connection.cursor() as cur:
        cur.execute(query, (list(symbols), adjust))
        rows = cur.fetchall()
    if not rows:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, "close": pl.Float64})
    return pl.DataFrame(rows, schema=["symbol", "date", "close"], orient="row")


def _load_mask(connection: psycopg.Connection, symbols: Sequence[str], *, resolution: str = "1M") -> pl.DataFrame:
    """Load PIT universe eligibility masks for given symbols and resolution."""
    query = """
        select symbol, cutoff_date, eligible
        from staging.universe_mask
        where symbol = any(%s) and resolution = %s
        order by cutoff_date asc
    """
    with connection.cursor() as cur:
        cur.execute(query, (list(symbols), resolution))
        rows = cur.fetchall()
    if not rows:
        return pl.DataFrame(schema={"symbol": pl.Utf8, "cutoff_date": pl.Date, "eligible": pl.Boolean})
    return pl.DataFrame(rows, schema=["symbol", "cutoff_date", "eligible"], orient="row")


def _resolve_snapshot_hash(
    connection: psycopg.Connection,
    universe_id: str,
    monthly_prices: pl.DataFrame,
    explicit_hash: str | None = None,
) -> str:
    """Resolve a deterministic data snapshot hash, strictly forbidding 'default_snapshot' (ADR A6)."""
    if explicit_hash and explicit_hash != "default_snapshot":
        return explicit_hash

    # 1. Try to read the newest served head run_id for this universe
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                select head_run_id from mart.served_head
                where universe_id like %s
                order by advanced_at desc
                limit 1
                """,
                (f"%{universe_id}%",),
            )
            row = cur.fetchone()
            if row and row[0]:
                return str(row[0])
    except Exception:
        pass

    # 2. Try to read from mart.current_pointer_head
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                select target_run_id from mart.current_pointer_head
                where universe_id like %s
                order by advanced_at desc
                limit 1
                """,
                (f"%{universe_id}%",),
            )
            row = cur.fetchone()
            if row and row[0]:
                return str(row[0])
    except Exception:
        pass

    # 3. Deterministic hash of dataset shape and boundaries
    if not monthly_prices.is_empty():
        min_date = str(monthly_prices["date"].min())
        max_date = str(monthly_prices["date"].max())
        n_rows = len(monthly_prices)
        raw_seed = f"{universe_id}:{n_rows}:{min_date}:{max_date}"
        return f"snap-{sha256(raw_seed.encode()).hexdigest()[:16]}"

    return f"snap-{sha256(universe_id.encode()).hexdigest()[:16]}"


def _load_fundamental_factors(connection: psycopg.Connection, symbols: Sequence[str], factor_name: str) -> pl.DataFrame:
    """Load point-in-time fundamental factor metrics for given symbols."""
    # 1. First check mart.topt_gppe_results if factor_name is gppe or related
    if factor_name in ("gppe", "operating_efficiency", "capital_adjusted_gross_profit"):
        query = f"""
            select distinct on (coalesce(k.identifier_value, upper(e.current_ticker)), date(g.cutoff))
                coalesce(k.identifier_value, upper(e.current_ticker)) as symbol,
                date(g.cutoff) as date,
                g.{factor_name}::float as factor_value
            from mart.topt_gppe_results g
            left join staging.kg_identifiers k on k.entity_id = g.issuer_id and k.identifier_type = 'ticker'
            left join mart.entity_identity e on (e.entity_id::text = g.issuer_id or e.legacy_id = g.issuer_id)
            where coalesce(k.identifier_value, upper(e.current_ticker)) = any(%s)
            order by coalesce(k.identifier_value, upper(e.current_ticker)), date(g.cutoff), g.cutoff desc
        """  # noqa: S608 - factor_name is one of three fixed literals
        try:
            with connection.cursor() as cur:
                cur.execute(query, (list(symbols),))
                rows = cur.fetchall()
            if rows:
                return pl.DataFrame(rows, schema=["symbol", "date", factor_name], orient="row")
        except Exception:
            try:
                query_fallback = f"""
                    select distinct on (k.identifier_value, date(g.cutoff))
                        k.identifier_value as symbol,
                        date(g.cutoff) as date,
                        g.{factor_name}::float as factor_value
                    from mart.topt_gppe_results g
                    join staging.kg_identifiers k on k.entity_id = g.issuer_id and k.identifier_type = 'ticker'
                    where k.identifier_value = any(%s)
                    order by k.identifier_value, date(g.cutoff), g.cutoff desc
                """
                with connection.cursor() as cur:
                    cur.execute(query_fallback, (list(symbols),))
                    rows = cur.fetchall()
                if rows:
                    return pl.DataFrame(rows, schema=["symbol", "date", factor_name], orient="row")
            except Exception:
                pass

    # 2. Check staging.strategy_backtest_inputs (gross_profit, headcount, revenue, total_assets, etc.)
    try:
        query_inputs = """
            select distinct on (coalesce(k.identifier_value, s.issuer_id), date(s.cutoff_at))
                coalesce(k.identifier_value, s.issuer_id) as symbol,
                date(s.cutoff_at) as date,
                s.value::float as factor_value
            from staging.strategy_backtest_inputs s
            left join staging.kg_identifiers k on k.entity_id = s.issuer_id and k.identifier_type = 'ticker'
            where coalesce(k.identifier_value, s.issuer_id) = any(%s)
              and s.input_key = %s
            order by coalesce(k.identifier_value, s.issuer_id), date(s.cutoff_at), s.knowable_at desc, s.recorded_at desc
        """
        with connection.cursor() as cur:
            cur.execute(query_inputs, (list(symbols), factor_name))
            rows = cur.fetchall()
        if rows:
            return pl.DataFrame(rows, schema=["symbol", "date", factor_name], orient="row")
    except Exception:
        pass

    return pl.DataFrame(schema={"symbol": pl.Utf8, "date": pl.Date, factor_name: pl.Float64})


def run_backtest(
    connection: psycopg.Connection,
    strategy_key: str,
    strategy_version: str,
    universe_id: str,
    symbols: Sequence[str],
    factor_column: str,
    top_k: int,
    dropout_k: int,
    data_snapshot_hash: str | None = None,
) -> BacktestResult:
    """Load staging market data, compile factor panel, simulate portfolio, and persist mart rows."""
    monthly_prices = _load_prices(connection, "staging.market_prices_monthly", symbols, adjust=SPLIT_ADJUSTED)
    if monthly_prices.is_empty():
        raise ValueError(
            f"no rows in staging.market_prices_monthly for {list(symbols)}; run market_data_refresh_pipeline first"
        )
    daily_prices = _load_prices(connection, "staging.market_prices_daily", symbols, adjust=SPLIT_ADJUSTED)
    mask = _load_mask(connection, symbols, resolution="1M")

    # If factor_column is not close, load fundamental factor metrics
    if factor_column != "close":
        fund_factors = _load_fundamental_factors(connection, symbols, factor_column)
        if not fund_factors.is_empty():
            # Align quarter-end / historical cutoffs to monthly trading dates strictly without lookahead
            monthly_prices = monthly_prices.sort(["date", "symbol"])
            fund_factors = fund_factors.sort(["date", "symbol"])
            monthly_prices = monthly_prices.join_asof(
                fund_factors,
                on="date",
                by="symbol",
                strategy="backward",
            )
        else:
            monthly_prices = monthly_prices.with_columns(pl.lit(None, dtype=pl.Float64).alias(factor_column))

    factor_expr = col(factor_column).rank()
    panel = compile_factor_panel(factor_expr, monthly_prices, mask_df=mask if not mask.is_empty() else None)
    weights = compute_topk_dropout_weights(
        panel, top_k=top_k, dropout_k=dropout_k, mask_df=mask if not mask.is_empty() else None
    )
    close_monthly, weights_monthly = pivot_to_vbt_matrices(weights, monthly_prices)

    close_daily: pd.DataFrame | None = None
    if not daily_prices.is_empty():
        close_daily, _ = pivot_to_vbt_matrices(weights, daily_prices)

    resolved_snapshot_hash = _resolve_snapshot_hash(connection, universe_id, monthly_prices, data_snapshot_hash)

    engine = NumpySimulationEngine(
        BacktestEngineConfig(
            top_k=top_k,
            dropout_k=dropout_k,
            data_snapshot_hash=resolved_snapshot_hash,
        )
    )
    result = engine.run(
        strategy_key=strategy_key,
        strategy_version=strategy_version,
        universe_id=universe_id,
        close_monthly=close_monthly,
        weights_monthly=weights_monthly,
        close_daily=close_daily,
        top_k=top_k,
        dropout_k=dropout_k,
        data_snapshot_hash=resolved_snapshot_hash,
    )
    persist_backtest_result(connection, result)
    return result
