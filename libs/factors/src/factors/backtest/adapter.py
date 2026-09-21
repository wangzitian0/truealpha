"""Rebalancing and VectorBT Adapter for factor panels and target weights."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import polars as pl

from factors.expressions.compiler import compile_to_polars


def _to_pandas(df: pl.DataFrame | pd.DataFrame) -> pd.DataFrame:
    """Convert Polars DataFrame to Pandas DataFrame without pyarrow requirement."""
    if isinstance(df, pd.DataFrame):
        return df.copy()
    try:
        return df.to_pandas()
    except (ModuleNotFoundError, Exception):
        return pd.DataFrame(df.to_dict(as_series=False))


def _to_polars(df: pl.DataFrame | pd.DataFrame) -> pl.DataFrame:
    """Convert Pandas DataFrame to Polars DataFrame."""
    if isinstance(df, pl.DataFrame):
        return df.clone()
    return pl.from_pandas(df)


def _find_date_col(columns: list[str]) -> str:
    for candidate in ("date", "cutoff", "cutoff_date", "timestamp"):
        if candidate in columns:
            return candidate
    return columns[0]


def compile_factor_panel(
    factor_expr: Any,
    price_df: pl.DataFrame | pd.DataFrame,
    mask_df: pl.DataFrame | pd.DataFrame | None = None,
) -> pl.DataFrame:
    """Compile and compute a factor expression over a price panel.

    Parameters:
    - factor_expr: AST node (e.g. from truealpha_contracts.ast or factors.expressions.dsl)
      or a raw polars.Expr or string column name.
    - price_df: Long-format DataFrame containing at least 'symbol', 'date' (or 'cutoff'),
      and price/feature columns.
    - mask_df: Optional long-format universe mask DataFrame with 'symbol',
      'cutoff_date' (or 'date'), and boolean 'eligible' column.

    Returns:
    - pl.DataFrame with calculated 'factor_value' and prior sorting on ['symbol', 'date'].
      If mask_df is provided, ineligible entries have factor_value set to None.
    """
    pdf = _to_polars(price_df)
    date_col = _find_date_col(pdf.columns)

    # Ensure both date and cutoff columns exist for both time-series and cross-sectional ops
    if "cutoff" not in pdf.columns and date_col in pdf.columns:
        pdf = pdf.with_columns(cutoff=pl.col(date_col))
    if "date" not in pdf.columns and "cutoff" in pdf.columns:
        pdf = pdf.with_columns(date=pl.col("cutoff"))

    # Prior sorting on ['symbol', 'date'] is required for time-series operations (Ref, Mean, Std)
    sort_cols = [c for c in ["symbol", "date"] if c in pdf.columns]
    pdf = pdf.sort(sort_cols)

    # Compile and evaluate factor expression
    compiled = compile_to_polars(factor_expr, symbol_col="symbol", date_col="date", cutoff_col="cutoff")
    pdf = pdf.with_columns(factor_value=compiled)

    # Also alias factor_val for compatibility
    pdf = pdf.with_columns(factor_val=pl.col("factor_value"))

    # Apply dynamic universe mask if provided
    if mask_df is not None:
        mdf = _to_polars(mask_df)
        m_date_col = _find_date_col(mdf.columns)
        join_right_cols = ["symbol", m_date_col, "eligible"]
        avail_right_cols = [c for c in join_right_cols if c in mdf.columns]
        mdf_sub = mdf.select(avail_right_cols)

        # Join on symbol and date
        pdf = pdf.join(
            mdf_sub,
            left_on=["symbol", "date"],
            right_on=["symbol", m_date_col],
            how="left",
        )
        if "eligible" in pdf.columns:
            pdf = pdf.with_columns(
                pl.when(pl.col("eligible") == True)  # noqa: E712
                .then(pl.col("factor_value"))
                .otherwise(None)
                .alias("factor_value"),
                pl.when(pl.col("eligible") == True)  # noqa: E712
                .then(pl.col("factor_val"))
                .otherwise(None)
                .alias("factor_val"),
            )

    return pdf


def compute_topk_dropout_weights(
    factor_df: pl.DataFrame | pd.DataFrame,
    top_k: int = 5,
    dropout_k: int = 2,
    mask_df: pl.DataFrame | pd.DataFrame | None = None,
) -> pl.DataFrame:
    """Evaluate monthly cutoffs and calculate Top-K Dropout target weights.

    - Evaluates cutoffs chronologically.
    - Applies dynamic universe mask where eligible == True (and non-null factor).
    - Top-K selection with dropout_k turnover limit:
      In initial period, selects top_k eligible assets with highest factor values.
      In subsequent periods, drops at most dropout_k out-of-top-k held assets to replace
      with highest-ranked new candidates, while retaining held assets that stay within top_k
      or within the dropout threshold.
    - Ineligible assets are dropped immediately regardless of dropout_k.
    - Assigns equal weights normalized to 1.0 (ineligible or non-selected get 0.0).
    """
    fdf = _to_polars(factor_df)
    cutoff_col = _find_date_col(fdf.columns)

    if "cutoff" not in fdf.columns:
        fdf = fdf.with_columns(cutoff=pl.col(cutoff_col))
    if "date" not in fdf.columns:
        fdf = fdf.with_columns(date=pl.col(cutoff_col))

    factor_val_col = "factor_value" if "factor_value" in fdf.columns else "factor_val"
    if factor_val_col not in fdf.columns:
        # Check any float column not in symbol, date, cutoff
        for c in fdf.columns:
            if c not in ("symbol", "date", "cutoff", "cutoff_date", "eligible"):
                factor_val_col = c
                break

    # Merge external mask if provided and not yet joined
    if mask_df is not None and "eligible" not in fdf.columns:
        mdf = _to_polars(mask_df)
        m_date_col = _find_date_col(mdf.columns)
        fdf = fdf.join(
            mdf.select([c for c in ["symbol", m_date_col, "eligible"] if c in mdf.columns]),
            left_on=["symbol", cutoff_col],
            right_on=["symbol", m_date_col],
            how="left",
        )

    # Sort cutoffs chronologically
    cutoffs = fdf[cutoff_col].unique().sort().to_list()

    current_held: list[str] = []
    records: list[dict[str, Any]] = []

    for c in cutoffs:
        cutoff_slice = fdf.filter(pl.col(cutoff_col) == c)
        rows = cutoff_slice.to_dicts()

        # Identify eligible candidates with non-null factor values
        candidates: list[tuple[str, float]] = []
        for r in rows:
            sym = str(r["symbol"])
            val = r.get(factor_val_col)
            eligible = r.get("eligible", True)
            if eligible is None:
                eligible = True
            if bool(eligible) and val is not None and not (isinstance(val, float) and np.isnan(val)):
                candidates.append((sym, float(val)))

        # Sort descending by factor value
        candidates.sort(key=lambda item: item[1], reverse=True)
        candidate_symbols = [item[0] for item in candidates]
        candidate_ranks = {sym: idx for idx, sym in enumerate(candidate_symbols)}

        k = min(top_k, len(candidate_symbols))
        if k == 0:
            selected: list[str] = []
            current_held = []
        else:
            # Check previously held assets: only keep if still eligible and have valid factor
            held_eligible = [s for s in current_held if s in candidate_ranks]

            # Partition into held assets in top_k vs out of top_k
            in_topk_held = [s for s in held_eligible if candidate_ranks[s] < k]
            out_of_topk_held = [s for s in held_eligible if candidate_ranks[s] >= k]
            # Order out-of-top-k held by rank ascending (best rank first, worst rank last)
            out_of_topk_held.sort(key=lambda s: candidate_ranks[s])

            # Drop at most dropout_k worst out-of-top-k held assets
            eff_dropout = k if (dropout_k is None or dropout_k >= k) else max(0, dropout_k)
            num_to_drop = min(len(out_of_topk_held), eff_dropout)
            num_to_keep = len(out_of_topk_held) - num_to_drop
            kept_out_of_topk = out_of_topk_held[:num_to_keep]

            retained = in_topk_held + kept_out_of_topk

            # Fill remaining slots up to k with the top available candidates
            slots_needed = k - len(retained)
            retained_set = set(retained)
            new_additions: list[str] = []
            for sym in candidate_symbols:
                if len(new_additions) >= slots_needed:
                    break
                if sym not in retained_set:
                    new_additions.append(sym)

            selected = retained + new_additions
            current_held = selected

        selected_set = set(selected)
        weight_per_asset = 1.0 / len(selected) if selected else 0.0

        for r in rows:
            sym = str(r["symbol"])
            w = weight_per_asset if sym in selected_set else 0.0
            r_copy = dict(r)
            r_copy["target_weight"] = w
            r_copy["weight"] = w
            records.append(r_copy)

    return pl.DataFrame(records)


def pivot_to_vbt_matrices(
    weights_df: pl.DataFrame | pd.DataFrame,
    prices_df: pl.DataFrame | pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Transform long-format DataFrames into wide-format Pandas DataFrames for VectorBT.

    Returns:
    - (close_matrix, weights_matrix):
      Index: sorted DatetimeIndex of trading/cutoff dates.
      Columns: sorted list of symbols, strictly identical in both matrices.
      Missing weights are filled with 0.0.
      Strictly asserts: assert (close_matrix.columns == weights_matrix.columns).all()
    """
    w_pdf = _to_pandas(weights_df)
    p_pdf = _to_pandas(prices_df)

    p_date_col = _find_date_col(list(p_pdf.columns))
    w_date_col = _find_date_col(list(w_pdf.columns))

    p_val_col = "close" if "close" in p_pdf.columns else "price"
    if p_val_col not in p_pdf.columns:
        for c in p_pdf.columns:
            if c not in ("symbol", p_date_col, "date", "cutoff", "cutoff_date"):
                p_val_col = c
                break

    w_val_col = "target_weight" if "target_weight" in w_pdf.columns else "weight"
    if w_val_col not in w_pdf.columns:
        for c in w_pdf.columns:
            if c not in ("symbol", w_date_col, "date", "cutoff", "cutoff_date"):
                w_val_col = c
                break

    # Convert dates to string representation before pivot to avoid unhashable type issues
    p_pdf["_date_key"] = pd.to_datetime(p_pdf[p_date_col])
    w_pdf["_date_key"] = pd.to_datetime(w_pdf[w_date_col])

    close_matrix = p_pdf.pivot(index="_date_key", columns="symbol", values=p_val_col)
    weights_matrix = w_pdf.pivot(index="_date_key", columns="symbol", values=w_val_col)

    # Determine unified set of symbols sorted alphabetically
    all_symbols = sorted(list(set(close_matrix.columns).union(set(weights_matrix.columns))))

    # Align columns and reindex index
    close_matrix = close_matrix.reindex(columns=all_symbols)
    weights_matrix = weights_matrix.reindex(index=close_matrix.index, columns=all_symbols).fillna(0.0)

    # Ensure index name and DatetimeIndex
    close_matrix.index.name = "date"
    weights_matrix.index.name = "date"
    close_matrix.index = pd.DatetimeIndex(close_matrix.index)
    weights_matrix.index = pd.DatetimeIndex(weights_matrix.index)

    close_matrix = close_matrix.sort_index()
    weights_matrix = weights_matrix.sort_index()

    # Strict column order equality assertion
    assert (close_matrix.columns == weights_matrix.columns).all(), (
        f"Column order mismatch: {list(close_matrix.columns)} vs {list(weights_matrix.columns)}"
    )

    return close_matrix, weights_matrix


__all__ = [
    "compile_factor_panel",
    "compute_topk_dropout_weights",
    "pivot_to_vbt_matrices",
]
