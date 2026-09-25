"""Capture and materialize analyst ratings for issuers (#771, init.md §0 q4).

Source: moomoo / OpenD get_analyst_consensus / get_rating_summary.
Materialized table: mart.issuer_analyst_ratings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from factors.base.analyst_track_record import (
    AnalystRatingItem,
    AnalystTrackRecord,
    analyst_track_record,
)
from psycopg import Connection

__all__ = (
    "AnalystRatingItem",
    "AnalystTrackRecord",
    "analyst_track_record",
    "capture_ticker_analyst_ratings",
    "materialize_analyst_ratings",
    "materialize_universe_analyst_ratings",
)

_INSERT_SQL = """
insert into mart.issuer_analyst_ratings (
    run_id,
    issuer_id,
    cutoff,
    consensus_rating,
    analysts_count,
    buy_count,
    hold_count,
    sell_count,
    confidence,
    reason_codes,
    extractor,
    availability_status,
    source_evidence_status,
    factor_validation_status
) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (run_id, issuer_id) do update set
    cutoff = excluded.cutoff,
    consensus_rating = excluded.consensus_rating,
    analysts_count = excluded.analysts_count,
    buy_count = excluded.buy_count,
    hold_count = excluded.hold_count,
    sell_count = excluded.sell_count,
    confidence = excluded.confidence,
    reason_codes = excluded.reason_codes,
    extractor = excluded.extractor,
    availability_status = excluded.availability_status,
    source_evidence_status = excluded.source_evidence_status,
    factor_validation_status = excluded.factor_validation_status
"""


def capture_ticker_analyst_ratings(
    ctx: Any,
    *,
    ticker: str,
    company_id: str,
    connection: Connection[Any],
    run_id: str,
    cutoff: datetime | None = None,
    raw_store: Any | None = None,
) -> int:
    """Capture analyst consensus for a single ticker via moomoo API and persist.

    Args:
        ctx: OpenQuoteContext or mock for moomoo API.
        ticker: Ticker symbol, e.g. 'AAPL' or 'US.AAPL'.
        company_id: Canonical issuer/company ID, e.g. 'issuer:lei:...'.
        connection: PostgreSQL connection.
        run_id: Governed run ID.
        cutoff: As-of cutoff timestamp.
        raw_store: Optional raw evidence object store.

    Returns:
        Number of rows inserted (1 on success).
    """
    as_of = cutoff or datetime.now(tz=UTC)
    code = f"US.{ticker}" if not ticker.startswith("US.") else ticker
    try:
        from data_engine.sources.moomoo import get_analyst_consensus

        ret, df = get_analyst_consensus(ctx, code, caller="capture_ticker_analyst_ratings")
        if ret == 0 and df is not None and not df.empty:
            row = df.iloc[0]
            raw_rating = row.get("consensus_rating")
            consensus_rating = Decimal(str(raw_rating)) if raw_rating is not None and not pd.isna(raw_rating) else None
            raw_count = row.get("analyst_count", row.get("recommend_num", 0))
            count = int(raw_count) if raw_count is not None and not pd.isna(raw_count) else 0

            # If consensus_rating is None or count <= 0, there is no real analyst coverage
            if consensus_rating is None or count <= 0:
                record = analyst_track_record([], entity_id=company_id, as_of=as_of)
                return materialize_analyst_ratings(
                    connection,
                    run_id=run_id,
                    cutoff=as_of,
                    ratings_data=[record],
                )

            # Construct ratings items and evaluate factor
            rating_val = int(round(float(consensus_rating)))
            rating_val = max(1, min(5, rating_val))
            items = [
                AnalystRatingItem(
                    analyst_id=f"moomoo:{ticker}:{i}",
                    rating=rating_val,
                    confidence=Decimal("0.85"),
                )
                for i in range(count)
            ]
            record = analyst_track_record(items, entity_id=company_id, as_of=as_of)
            return materialize_analyst_ratings(
                connection,
                run_id=run_id,
                cutoff=as_of,
                ratings_data=[record],
            )
        else:
            record = analyst_track_record([], entity_id=company_id, as_of=as_of)
            return materialize_analyst_ratings(
                connection,
                run_id=run_id,
                cutoff=as_of,
                ratings_data=[record],
            )
    except Exception as exc:
        record = analyst_track_record([], entity_id=company_id, as_of=as_of)
        return materialize_analyst_ratings(
            connection,
            run_id=run_id,
            cutoff=as_of,
            ratings_data=[
                {
                    "issuer_id": company_id,
                    "consensus_rating": None,
                    "analysts_count": 0,
                    "buy_count": 0,
                    "hold_count": 0,
                    "sell_count": 0,
                    "confidence": Decimal("0"),
                    "availability_status": "unavailable",
                    "source_evidence_status": "degraded",
                    "factor_validation_status": "not_evaluated",
                    "reason_codes": [f"fetch_error:{type(exc).__name__}"],
                }
            ],
        )


def materialize_analyst_ratings(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime | None = None,
    ratings_data: Sequence[dict[str, Any] | AnalystTrackRecord],
) -> int:
    """Materialize batch analyst rating rows into mart.issuer_analyst_ratings."""
    as_of = cutoff or datetime.now(tz=UTC)
    count = 0
    for item in ratings_data:
        if isinstance(item, AnalystTrackRecord):
            issuer_id = item.entity_id
            consensus_rating = item.consensus_rating
            analysts_count = item.ratings_count
            buy_count = item.buy_count
            hold_count = item.hold_count
            sell_count = item.sell_count
            confidence = item.result.confidence
            reason_codes = list(item.result.flags)
            extractor = "origin:moomoo:v1"
            avail = (
                "available"
                if item.result.data_availability == "verified" and consensus_rating is not None
                else "unavailable"
            )
            source_status = "verified" if avail == "available" else "degraded"
            val_status = "accepted" if consensus_rating is not None else "not_evaluated"
        else:
            issuer_id = str(item["issuer_id"])
            if "ratings" in item:
                rec = analyst_track_record(item["ratings"], entity_id=issuer_id, as_of=as_of)
                consensus_rating = rec.consensus_rating
                analysts_count = rec.ratings_count
                buy_count = rec.buy_count
                hold_count = rec.hold_count
                sell_count = rec.sell_count
                confidence = rec.result.confidence
                reason_codes = list(rec.result.flags)
                avail = (
                    "available"
                    if rec.result.data_availability == "verified" and consensus_rating is not None
                    else "unavailable"
                )
                source_status = "verified" if avail == "available" else "degraded"
                val_status = "accepted" if consensus_rating is not None else "not_evaluated"
            else:
                raw_c = item.get("consensus_rating")
                consensus_rating = Decimal(str(raw_c)) if raw_c is not None else None
                analysts_count = int(item.get("analysts_count", 0))
                buy_count = int(item.get("buy_count", 0))
                hold_count = int(item.get("hold_count", 0))
                sell_count = int(item.get("sell_count", 0))
                raw_conf = item.get("confidence")
                if raw_conf is not None:
                    confidence = Decimal(str(raw_conf))
                else:
                    confidence = Decimal("0.8") if consensus_rating is not None else Decimal("0")
                reason_codes = list(item.get("reason_codes", []))
                avail = str(
                    item.get("availability_status", "available" if consensus_rating is not None else "unavailable")
                )
                source_status = str(
                    item.get("source_evidence_status", "verified" if avail == "available" else "degraded")
                )
                val_status = str(
                    item.get(
                        "factor_validation_status", "accepted" if consensus_rating is not None else "not_evaluated"
                    )
                )
            extractor = str(item.get("extractor", "origin:moomoo:v1"))

        connection.execute(
            _INSERT_SQL,
            (
                run_id,
                issuer_id,
                as_of,
                consensus_rating,
                analysts_count,
                buy_count,
                hold_count,
                sell_count,
                confidence,
                reason_codes,
                extractor,
                avail,
                source_status,
                val_status,
            ),
        )
        count += 1
    return count


def materialize_universe_analyst_ratings(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime,
    tickers: Mapping[str, str],
    ctx: Any | None = None,
) -> int:
    """Capture and materialize analyst ratings for all issuers in a universe run."""
    count = 0
    for issuer_id, ticker in tickers.items():
        count += capture_ticker_analyst_ratings(
            ctx,
            ticker=ticker,
            company_id=issuer_id,
            connection=connection,
            run_id=run_id,
            cutoff=cutoff,
        )
    return count
