"""Capture and materialize analyst ratings for issuers (#771, init.md §0 q4).

Source: moomoo / OpenD `get_research_analyst_consensus`.
Materialized table: mart.issuer_analyst_ratings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

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


_RATING_MIN = 1
_RATING_MAX = 5


def _count_from_share(total: int, share: float | None) -> int:
    """Analysts behind a rating, from moomoo's share in percent (12.34 means 12.34 percent)."""
    if share is None:
        return 0
    percent = float(share)
    if not 0 <= percent <= 100:
        raise ValueError(f"analyst share {percent} is outside 0 to 100 percent")
    return round(total * percent / 100)


def _consensus_row(payload: object, company_id: str) -> dict[str, Any] | None:
    """Turn one `get_research_analyst_consensus` payload into a rating row.

    Return None when the payload holds no consensus: no rating, rating 0 (unknown), or no
    analysts. The wrapper `get_analyst_consensus` returns the payload alone, without the
    return code. The SDK builds the payload as a dict and omits each field moomoo leaves unset.
    Fields: `rating` is moomoo ResearchRatingType (1 to 5, 0 is unknown), `total` is the
    analyst count of the last 3 months, `buy`, `hold` and `sell` are shares in percent.
    """
    if not isinstance(payload, Mapping):
        raise TypeError(f"get_research_analyst_consensus returned {type(payload).__name__}, expected a mapping")
    rating = payload.get("rating")
    total = payload.get("total")
    if rating is None or total is None or rating == 0 or int(total) <= 0:
        return None
    if not _RATING_MIN <= int(rating) <= _RATING_MAX:
        raise ValueError(f"moomoo rating {rating!r} is outside {_RATING_MIN} to {_RATING_MAX}")
    count = int(total)
    return {
        "issuer_id": company_id,
        "consensus_rating": Decimal(int(rating)),
        "analysts_count": count,
        "buy_count": _count_from_share(count, payload.get("buy")),
        "hold_count": _count_from_share(count, payload.get("hold")),
        "sell_count": _count_from_share(count, payload.get("sell")),
        "confidence": Decimal("0.85"),
        "availability_status": "available",
        "source_evidence_status": "verified",
        "factor_validation_status": "accepted",
        "reason_codes": [],
    }


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
    if ctx is None:
        record = analyst_track_record([], entity_id=company_id, as_of=as_of)
        return materialize_analyst_ratings(
            connection,
            run_id=run_id,
            cutoff=as_of,
            ratings_data=[record],
        )

    code = f"US.{ticker}" if not ticker.startswith("US.") else ticker
    try:
        from data_engine.sources.moomoo import get_analyst_consensus

        payload = get_analyst_consensus(ctx, code, caller="capture_ticker_analyst_ratings")
        consensus_row = _consensus_row(payload, company_id)
    except Exception as exc:
        ratings_data = [
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
        ]
    else:
        if consensus_row is None:
            ratings_data = [analyst_track_record([], entity_id=company_id, as_of=as_of)]
        else:
            ratings_data = [consensus_row]

    return materialize_analyst_ratings(
        connection,
        run_id=run_id,
        cutoff=as_of,
        ratings_data=ratings_data,
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
