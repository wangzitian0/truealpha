"""Capture and materialize analyst ratings for issuers (#771, init.md §0 q4)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from numbers import Real
from typing import Any

from factors.base.analyst_track_record import AnalystRatingItem, AnalystTrackRecord, analyst_track_record
from psycopg import Connection

from data_engine.datahub._helpers import (
    safe_case_id_part,
    single_origin_confidence,
    unbacked_status_values,
)
from data_engine.datahub.canonical_issuer import require_canonical_issuer_id
from data_engine.datahub.fetch_key import FetchKey
from data_engine.jsonable import is_dataframe

__all__ = (
    "MAX_FAILURES_LOGGED",
    "AnalystRatingItem",
    "AnalystTrackRecord",
    "FetchFailure",
    "UniverseCapture",
    "analyst_track_record",
    "materialize_analyst_ratings",
    "materialize_universe_analyst_ratings",
    "parse_rating_summary",
)

log = logging.getLogger(__name__)

_INSERT_SQL = (
    "insert into mart.issuer_analyst_ratings ("
    "run_id, issuer_id, cutoff, consensus_rating, analysts_count, buy_count, hold_count, sell_count, "
    "confidence, reason_codes, extractor, availability_status, source_evidence_status, factor_validation_status"
    ") values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict (run_id, issuer_id) do update set "
    "cutoff = excluded.cutoff, consensus_rating = excluded.consensus_rating, analysts_count = excluded.analysts_count, "
    "buy_count = excluded.buy_count, hold_count = excluded.hold_count, sell_count = excluded.sell_count, "
    "confidence = excluded.confidence, reason_codes = excluded.reason_codes, extractor = excluded.extractor, "
    "availability_status = excluded.availability_status, source_evidence_status = excluded.source_evidence_status, "
    "factor_validation_status = excluded.factor_validation_status "
    "where not (mart.issuer_analyst_ratings.availability_status = 'available' "
    "and excluded.availability_status <> 'available' "
    "and exists (select 1 from unnest(excluded.reason_codes) as code where starts_with(code, 'fetch_error:')))"
)

_RATING_MIN = 1
_RATING_MAX = 5
_RATING_UNKNOWN = 0
MAX_FAILURES_LOGGED = 20


@dataclass(frozen=True)
class FetchFailure:
    ticker: str
    error: str


@dataclass(frozen=True)
class UniverseCapture:
    rows: int
    failures: tuple[FetchFailure, ...] = ()

    def lane_failure(self) -> str | None:
        if self.rows > 0 and len(self.failures) == self.rows:
            first = self.failures[0]
            return f"analyst ratings fetch failed for {len(self.failures)} of {self.rows} tickers; first error: {first.ticker}: {first.error}"
        return None


def _moomoo_rating_confidence(
    case_id: str,
    evidence_id: str,
    *,
    agreement: Decimal = Decimal("1"),
    sample_conformance: Decimal = Decimal("1"),
    reason: str = "reason:single_origin",
) -> Decimal:
    return single_origin_confidence(
        case_id=case_id,
        provider_id="origin:moomoo-ratings:v1",
        lineage="moomoo",
        evidence_id=evidence_id,
        agreement=agreement,
        sample_conformance=sample_conformance,
        reason_codes=(reason,),
    )


def _finite_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (Decimal, Real)):
        return None
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    return dec if dec.is_finite() else None


def _count_from_share(total: int, share: object) -> int:
    if share is None:
        return 0
    percent = _finite_decimal(share)
    if percent is None:
        raise ValueError(f"analyst share {share!r} is not a number")
    if not Decimal("0") <= percent <= Decimal("100"):
        raise ValueError(f"analyst share {percent} is outside 0 to 100 percent")
    return int((Decimal(total) * percent / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _is_empty(payload: object) -> bool:
    if payload is None:
        return True
    if is_dataframe(payload):
        return bool(payload.empty)  # type: ignore[attr-defined]
    return isinstance(payload, (Mapping, list)) and not payload


def _whole_number(payload: Mapping[str, Any], key: str) -> int:
    value = payload[key]
    dec = _finite_decimal(value)
    if dec is None or dec != dec.to_integral_value():
        raise ValueError(f"moomoo {key} {value!r} is not a whole number")
    return int(dec)


def _optional_target(payload: Mapping[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        if (val := payload.get(key)) is not None and not isinstance(val, bool) and isinstance(val, Real):
            try:
                dec = Decimal(str(val))
                if dec.is_finite() and dec >= 0:
                    return dec
            except Exception:  # noqa: BLE001
                pass
    return None


def _target_stats(items: Sequence[AnalystRatingItem]) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    targets = [item.target_price for item in items if item.target_price is not None]
    if not targets:
        return None, None, None
    return max(targets), min(targets), sum(targets, start=Decimal(0)) / Decimal(len(targets))


def _consensus_confidence(company_id: str) -> Decimal:
    safe_company = safe_case_id_part(company_id, "issuer")
    return _moomoo_rating_confidence(f"analyst-consensus:{safe_company}", f"consensus:{safe_company}")


def _rating_row(
    issuer_id: str,
    consensus_rating: Decimal | None = None,
    analysts_count: int = 0,
    buy_count: int = 0,
    hold_count: int = 0,
    sell_count: int = 0,
    confidence: Decimal = Decimal("0"),
    availability_status: str = "unavailable",
    reason_codes: Sequence[str] = (),
    **extra: Any,
) -> dict[str, Any]:
    return {
        "issuer_id": issuer_id,
        "consensus_rating": consensus_rating,
        "analysts_count": analysts_count,
        "buy_count": buy_count,
        "hold_count": hold_count,
        "sell_count": sell_count,
        "confidence": confidence,
        "availability_status": availability_status,
        "reason_codes": list(reason_codes),
        **extra,
    }


def _consensus_row(payload: object, company_id: str) -> dict[str, Any] | None:
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
    return _rating_row(
        company_id,
        Decimal(rating),
        count,
        _count_from_share(count, payload.get("buy")),
        _count_from_share(count, payload.get("hold")),
        _count_from_share(count, payload.get("sell")),
        _consensus_confidence(company_id),
        "available",
        target_high=_optional_target(payload, "target_high", "highest"),
        target_low=_optional_target(payload, "target_low", "lowest"),
        target_mean=_optional_target(payload, "target_mean", "average"),
    )


def _analyst_confidence(analyst_id: str, raw_sr: object) -> Decimal:
    sr_factor = Decimal("0.85")
    sr_dec = _finite_decimal(raw_sr)
    if sr_dec is not None:
        clamped = max(Decimal("0"), min(Decimal("100"), sr_dec))
        sr_factor = (Decimal("0.70") + Decimal("0.30") * (clamped / Decimal("100"))).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
    safe_analyst = safe_case_id_part(analyst_id, "analyst")
    return _moomoo_rating_confidence(
        f"analyst:{safe_analyst}",
        f"analyst:{safe_analyst}",
        agreement=sr_factor,
        sample_conformance=Decimal("0.90"),
        reason="vendor_timestamp",
    )


def _valid_rating_point(r_item: object, cutoff_iso: str) -> tuple[str, int] | None:
    if not isinstance(r_item, Mapping):
        return None
    rec_date, raw_rating = r_item.get("recommendation_date_str"), r_item.get("rating")
    if not isinstance(rec_date, str) or len(rec_date) < 10 or rec_date[:10] > cutoff_iso:
        return None
    rating_dec = _finite_decimal(raw_rating)
    if rating_dec is None or rating_dec != rating_dec.to_integral_value():
        return None
    rating_int = int(rating_dec)
    return (rec_date[:10], rating_int) if _RATING_MIN <= rating_int <= _RATING_MAX else None


def parse_rating_summary(payload: object, *, as_of: datetime) -> list[AnalystRatingItem]:
    if (
        _is_empty(payload)
        or not isinstance(payload, Mapping)
        or not isinstance(summaries := payload.get("analyst_rating_summary_list"), list)
    ):
        return []
    cutoff_iso = as_of.date().isoformat()
    out: list[AnalystRatingItem] = []
    for entry in summaries:
        if not isinstance(entry, Mapping) or not isinstance(analyst_info := entry.get("analyst_info"), Mapping):
            continue
        analyst_id = str(analyst_info.get("analyst_id") or analyst_info.get("analyst_name") or "").strip()
        if not analyst_id or not isinstance(items := entry.get("rating_item_list"), list):
            continue
        points = [pt for item in items if (pt := _valid_rating_point(item, cutoff_iso)) is not None]
        if points:
            latest_date, latest_rating = max(points, key=lambda pt: pt[0])
            rating_item = AnalystRatingItem(
                analyst_id=analyst_id,
                rating=latest_rating,
                confidence=_analyst_confidence(analyst_id, analyst_info.get("success_rate")),
            )
            try:
                object.__setattr__(
                    rating_item, "recommendation_at", datetime.fromisoformat(latest_date).replace(tzinfo=UTC)
                )
            except ValueError:
                pass
            out.append(rating_item)
    return out


def _fetch_error_row(company_id: str, exc: Exception) -> dict[str, Any]:
    return _rating_row(company_id, reason_codes=[f"fetch_error:{type(exc).__name__}"])


def _research_payload(ctx: Any, code: str, endpoint: str, fn: Callable[..., Any], **kwargs: Any) -> Any:
    from data_engine.sources import gateway
    from data_engine.sources.moomoo import canonical_moomoo_bytes

    key = FetchKey("moomoo", endpoint, f"symbol:{code.removeprefix('US.')}", gateway.utc_day())
    return json.loads(
        gateway.fetch_once(
            key,
            lambda identity: canonical_moomoo_bytes(
                fn(ctx, code, caller="capture_ticker_analyst_ratings", identity=identity, **kwargs)
            ),
        ).decode()
    )


def _fetch_rating_enrichment(ctx: Any, code: str, *, as_of: datetime) -> list[AnalystRatingItem]:
    from data_engine.sources.moomoo import get_rating_summary

    if not hasattr(ctx, "get_research_rating_summary"):
        return []
    try:
        payload = _research_payload(ctx, code, "get_research_rating_summary", get_rating_summary)
        return parse_rating_summary(payload, as_of=as_of)
    except Exception:  # noqa: BLE001
        return []


def _ticker_row(
    ctx: Any,
    *,
    ticker: str,
    company_id: str,
    as_of: datetime,
    open_error: Exception | None,
    log_traceback: bool,
) -> tuple[dict[str, Any] | AnalystTrackRecord, FetchFailure | None]:
    if ctx is None:
        if open_error is None:
            return analyst_track_record([], entity_id=company_id, as_of=as_of), None
        return _fetch_error_row(company_id, open_error), FetchFailure(
            ticker, f"{type(open_error).__name__}: {open_error}"
        )
    code = ticker if ticker.startswith("US.") else f"US.{ticker}"
    try:
        from data_engine.sources.moomoo import get_analyst_consensus

        payload = _research_payload(ctx, code, "get_research_analyst_consensus", get_analyst_consensus)
        consensus_row = _consensus_row(payload, company_id)
        summary_items = _fetch_rating_enrichment(ctx, code, as_of=as_of)
    except Exception as exc:
        failure = FetchFailure(ticker=ticker, error=f"{type(exc).__name__}: {exc}")
        log.error(
            "analyst consensus fetch failed for %s (%s): %s", ticker, company_id, failure.error, exc_info=log_traceback
        )
        return _fetch_error_row(company_id, exc), failure
    if consensus_row is None and not summary_items:
        return analyst_track_record([], entity_id=company_id, as_of=as_of), None
    summary_rec = analyst_track_record(summary_items, entity_id=company_id, as_of=as_of) if summary_items else None
    high, low, mean = _target_stats(summary_items) if summary_items else (None, None, None)
    if consensus_row is None:
        assert summary_rec is not None
        return _rating_row(
            company_id,
            summary_rec.consensus_rating,
            summary_rec.ratings_count,
            summary_rec.buy_count,
            summary_rec.hold_count,
            summary_rec.sell_count,
            summary_rec.result.confidence,
            "available" if summary_rec.consensus_rating is not None else "unavailable",
            [*summary_rec.result.flags, "vendor_timestamp"],
            target_high=high,
            target_low=low,
            target_mean=mean,
            rating_items=list(summary_items),
        ), None
    if summary_items:
        assert summary_rec is not None
        if summary_rec.consensus_rating is not None:
            consensus_row["consensus_rating"] = summary_rec.consensus_rating
        for key, val in (("target_high", high), ("target_low", low), ("target_mean", mean)):
            if consensus_row.get(key) is None and val is not None:
                consensus_row[key] = val
        consensus_row["rating_items"] = list(summary_items)
        consensus_row["confidence"] = _moomoo_rating_confidence(
            f"consensus:{company_id}",
            f"consensus:{company_id}",
            agreement=min(item.confidence for item in summary_items),
            sample_conformance=Decimal("0.90"),
            reason="vendor_timestamp",
        )
        reasons = list(consensus_row["reason_codes"])
        if "vendor_timestamp" not in reasons:
            reasons.append("vendor_timestamp")
        consensus_row["reason_codes"] = reasons
    return consensus_row, None


def _unpack_track_record(rec: AnalystTrackRecord) -> tuple[Any, int, int, int, int, Decimal, list[str], str]:
    avail = "available" if not rec.result.flags and rec.consensus_rating is not None else "unavailable"
    return (
        rec.consensus_rating,
        rec.ratings_count,
        rec.buy_count,
        rec.hold_count,
        rec.sell_count,
        rec.result.confidence,
        list(rec.result.flags),
        avail,
    )


def materialize_analyst_ratings(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime | None = None,
    ratings_data: Sequence[dict[str, Any] | AnalystTrackRecord],
) -> int:
    as_of, source_status, val_status = unbacked_status_values(cutoff)
    for item in ratings_data:
        rating_items: Sequence[AnalystRatingItem] = ()
        if isinstance(item, AnalystTrackRecord):
            issuer_id, extractor = item.entity_id, "origin:moomoo:v1"
            fields = _unpack_track_record(item)
        else:
            issuer_id, extractor = str(item["issuer_id"]), str(item.get("extractor", "origin:moomoo:v1"))
            if "ratings" in item:
                rating_items = item["ratings"]
                fields = _unpack_track_record(analyst_track_record(rating_items, entity_id=issuer_id, as_of=as_of))
            else:
                if isinstance(item.get("rating_items"), Sequence):
                    rating_items = item["rating_items"]
                raw_c, raw_conf = item.get("consensus_rating"), item.get("confidence")
                consensus_rating = Decimal(str(raw_c)) if raw_c is not None else None
                confidence = (
                    Decimal(str(raw_conf))
                    if raw_conf is not None
                    else (Decimal("0.8") if consensus_rating is not None else Decimal("0"))
                )
                fields = (
                    consensus_rating,
                    int(item.get("analysts_count", 0)),
                    int(item.get("buy_count", 0)),
                    int(item.get("hold_count", 0)),
                    int(item.get("sell_count", 0)),
                    confidence,
                    list(item.get("reason_codes", [])),
                    str(
                        item.get("availability_status", "available" if consensus_rating is not None else "unavailable")
                    ),
                )
        canonical_id = require_canonical_issuer_id(issuer_id)
        connection.execute(
            _INSERT_SQL, (run_id, canonical_id, as_of, *fields[:-1], extractor, fields[-1], source_status, val_status)
        )
    return len(ratings_data)


def materialize_universe_analyst_ratings(
    connection: Connection[Any],
    *,
    run_id: str,
    cutoff: datetime,
    tickers: Mapping[str, str],
    ctx: Any | None = None,
    open_error: Exception | None = None,
) -> UniverseCapture:
    if ctx is None and open_error is not None:
        log.error(
            "analyst ratings fetch skipped, the moomoo context did not open: %s: %s",
            type(open_error).__name__,
            open_error,
        )
    fetched: list[dict[str, Any] | AnalystTrackRecord] = []
    failures: list[FetchFailure] = []
    for issuer_id, ticker in tickers.items():
        row, failure = _ticker_row(
            ctx,
            ticker=ticker,
            company_id=issuer_id,
            as_of=cutoff,
            open_error=open_error,
            log_traceback=len(failures) < MAX_FAILURES_LOGGED,
        )
        fetched.append(row)
        if failure is not None:
            failures.append(failure)
    rows = materialize_analyst_ratings(connection, run_id=run_id, cutoff=cutoff, ratings_data=fetched)
    return UniverseCapture(rows=rows, failures=tuple(failures))
