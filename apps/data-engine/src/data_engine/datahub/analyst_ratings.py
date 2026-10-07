"""Capture and materialize analyst ratings for issuers (#771, init.md §0 q4).

Source: moomoo / OpenD `get_research_analyst_consensus`.
Materialized table: mart.issuer_analyst_ratings.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from numbers import Real
from typing import Any

import pandas as pd
from factors.base.analyst_track_record import (
    AnalystRatingItem,
    AnalystTrackRecord,
    analyst_track_record,
)
from psycopg import Connection

from data_engine.datahub.canonical_issuer import require_canonical_issuer_id

__all__ = (
    "MAX_FAILURES_LOGGED",
    "AnalystRatingItem",
    "AnalystTrackRecord",
    "FetchFailure",
    "TickerCapture",
    "UniverseCapture",
    "analyst_track_record",
    "capture_ticker_analyst_ratings",
    "materialize_analyst_ratings",
    "materialize_universe_analyst_ratings",
)

log = logging.getLogger(__name__)

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
where not (
    mart.issuer_analyst_ratings.availability_status = 'available'
    and excluded.availability_status <> 'available'
    and exists (select 1 from unnest(excluded.reason_codes) as code where starts_with(code, 'fetch_error:'))
)
"""


_RATING_MIN = 1
_RATING_MAX = 5
_RATING_UNKNOWN = 0

#: The most fetch failures of one run that log a traceback. A run over a large universe in an
#: outage would otherwise write one traceback per ticker. The Dagster op logs the same number.
MAX_FAILURES_LOGGED = 20


@dataclass(frozen=True)
class FetchFailure:
    """One ticker whose consensus fetch raised. `error` reads `ExceptionType: message`."""

    ticker: str
    error: str


@dataclass(frozen=True)
class TickerCapture:
    """The outcome of one ticker: the rows persisted and the fetch failure, if any."""

    rows: int
    failure: FetchFailure | None = None


@dataclass(frozen=True)
class UniverseCapture:
    """The outcome of one universe run. The caller commits the rows, then reports
    `lane_failure` in the run summary. A later op raises it, after the coverage report."""

    rows: int
    failures: tuple[FetchFailure, ...] = ()

    def lane_failure(self) -> str | None:
        """The message of a total failure: every ticker of a non-empty run ended in a fetch error.

        Return None otherwise. A partial failure stays as unavailable rows with a reason code.
        """
        if self.rows > 0 and len(self.failures) == self.rows:
            first = self.failures[0]
            return (
                f"analyst ratings fetch failed for {len(self.failures)} of {self.rows} tickers; "
                f"first error: {first.ticker}: {first.error}"
            )
        return None


def _count_from_share(total: int, share: object) -> int:
    """Analysts behind a rating, from moomoo's share in percent (12.34 means 12.34 percent)."""
    if share is None:
        return 0
    if isinstance(share, bool) or not isinstance(share, Real):
        raise ValueError(f"analyst share {share!r} is not a number")
    percent = float(share)
    if not 0 <= percent <= 100:
        raise ValueError(f"analyst share {percent} is outside 0 to 100 percent")
    return round(total * percent / 100)


def _is_empty(payload: object) -> bool:
    """True for the answers that say "nothing here": None, an empty mapping, an empty frame."""
    if payload is None:
        return True
    if isinstance(payload, pd.DataFrame):
        return payload.empty
    return isinstance(payload, Mapping) and not payload


def _whole_number(payload: Mapping[str, Any], key: str) -> int:
    """The value of `key` as an int. A fraction, text, a bool or NaN is a contract error."""
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, Real) or not float(value).is_integer():
        raise ValueError(f"moomoo {key} {value!r} is not a whole number")
    return int(float(value))


def _consensus_row(payload: object, company_id: str) -> dict[str, Any] | None:
    """Turn one `get_research_analyst_consensus` payload into a rating row.

    Return None when the payload holds no consensus: an EMPTY payload, rating 0 (unknown) or total 0.
    A total of 0 is no coverage even when `rating` is missing.
    Raise ValueError or TypeError on a contract error. A non-empty payload without `total` is one.
    A total above 0 with no `rating` is one. So is a rating or total that is no whole number.
    A negative total is one. So is a rating out of range.
    The wrapper `get_analyst_consensus` returns the payload alone, without the return code.
    The SDK builds the payload as a dict and omits each field moomoo leaves unset.
    Fields: `rating` is moomoo ResearchRatingType (1 to 5, 0 is unknown), `total` is the
    analyst count of the last 3 months, `buy`, `hold` and `sell` are shares in percent.
    """
    if _is_empty(payload):
        return None
    if not isinstance(payload, Mapping):
        raise TypeError(f"get_research_analyst_consensus returned {type(payload).__name__}, expected a mapping")
    count = None if payload.get("total") is None else _whole_number(payload, "total")
    if count is not None and count < 0:
        raise ValueError(f"moomoo total {count} is negative")
    if count == 0:
        return None
    if count is None or payload.get("rating") is None:
        missing = [key for key in ("rating", "total") if payload.get(key) is None]
        raise ValueError(
            f"get_research_analyst_consensus payload lacks {' and '.join(missing)}; keys: {sorted(map(str, payload))}"
        )
    rating = _whole_number(payload, "rating")
    if not _RATING_MIN <= rating <= _RATING_MAX:
        if rating == _RATING_UNKNOWN:
            return None
        raise ValueError(f"moomoo rating {rating!r} is outside {_RATING_MIN} to {_RATING_MAX}")
    return {
        "issuer_id": company_id,
        "consensus_rating": Decimal(rating),
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


def _fetch_error_row(company_id: str, exc: Exception) -> dict[str, Any]:
    """The unavailable row for an issuer whose fetch failed. The reason code holds the exception
    type only, never its text, which can name a host."""
    return {
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


def capture_ticker_analyst_ratings(
    ctx: Any,
    *,
    ticker: str,
    company_id: str,
    connection: Connection[Any],
    run_id: str,
    cutoff: datetime | None = None,
    raw_store: Any | None = None,
    open_error: Exception | None = None,
    log_traceback: bool = True,
) -> TickerCapture:
    """Capture analyst consensus for a single ticker via moomoo API and persist.

    Args:
        ctx: OpenQuoteContext or mock for moomoo API.
        ticker: Ticker symbol, e.g. 'AAPL' or 'US.AAPL'.
        company_id: The issuer id of the wide row, a UUID. The write refuses any other form (#1079).
        connection: PostgreSQL connection.
        run_id: Governed run ID.
        cutoff: As-of cutoff timestamp.
        raw_store: Optional raw evidence object store.
        open_error: Why `ctx` is None: the exception that stopped the context from opening.
            The ticker then fails like a fetch error. Without it, `ctx=None` means no coverage.
        log_traceback: Log a failed fetch with its traceback. False logs the message alone.

    Returns:
        The rows inserted (1) and the fetch failure, if the fetch raised. A fetch failure
        is logged at ERROR level with the ticker, the message and the traceback. It is
        persisted as an unavailable row with the reason code `fetch_error:<ExceptionType>`.
    """
    as_of = cutoff or datetime.now(tz=UTC)
    failure: FetchFailure | None = None
    ratings_data: list[dict[str, Any] | AnalystTrackRecord]
    if ctx is None:
        if open_error is None:
            ratings_data = [analyst_track_record([], entity_id=company_id, as_of=as_of)]
        else:
            failure = FetchFailure(ticker=ticker, error=f"{type(open_error).__name__}: {open_error}")
            ratings_data = [_fetch_error_row(company_id, open_error)]
    else:
        code = f"US.{ticker}" if not ticker.startswith("US.") else ticker
        try:
            from data_engine.sources.moomoo import get_analyst_consensus

            payload = get_analyst_consensus(ctx, code, caller="capture_ticker_analyst_ratings")
            consensus_row = _consensus_row(payload, company_id)
        except Exception as exc:
            failure = FetchFailure(ticker=ticker, error=f"{type(exc).__name__}: {exc}")
            log.error(
                "analyst consensus fetch failed for %s (%s): %s",
                ticker,
                company_id,
                failure.error,
                exc_info=log_traceback,
            )
            ratings_data = [_fetch_error_row(company_id, exc)]
        else:
            if consensus_row is None:
                ratings_data = [analyst_track_record([], entity_id=company_id, as_of=as_of)]
            else:
                ratings_data = [consensus_row]

    rows = materialize_analyst_ratings(
        connection,
        run_id=run_id,
        cutoff=as_of,
        ratings_data=ratings_data,
    )
    return TickerCapture(rows=rows, failure=failure)


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
                require_canonical_issuer_id(issuer_id),
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
    open_error: Exception | None = None,
) -> UniverseCapture:
    """Capture and materialize analyst ratings for all issuers in a universe run.

    `tickers` maps the wide row's issuer id to the ticker. `canonicalize_universe` builds it.
    The caller owns the transaction. Commit the rows first, then pass
    `UniverseCapture.lane_failure()` on in the run summary.
    Only the first `MAX_FAILURES_LOGGED` failed fetches log a traceback; later ones log the message.

    Pass `open_error` when the moomoo context could not be opened. Every ticker then fails
    with `fetch_error:<ExceptionType>`, so an OpenD outage is a total failure, not no coverage.
    """
    if ctx is None and open_error is not None:
        log.error(
            "analyst ratings fetch skipped, the moomoo context did not open: %s: %s",
            type(open_error).__name__,
            open_error,
        )
    rows = 0
    failures: list[FetchFailure] = []
    for issuer_id, ticker in tickers.items():
        captured = capture_ticker_analyst_ratings(
            ctx,
            ticker=ticker,
            company_id=issuer_id,
            connection=connection,
            run_id=run_id,
            cutoff=cutoff,
            open_error=open_error,
            log_traceback=len(failures) < MAX_FAILURES_LOGGED,
        )
        rows += captured.rows
        if captured.failure is not None:
            failures.append(captured.failure)
    return UniverseCapture(rows=rows, failures=tuple(failures))
