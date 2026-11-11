"""Capture and materialize analyst ratings for issuers (#771, init.md §0 q4)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from numbers import Real
from typing import Any

import pandas as pd
from factors.base.analyst_track_record import AnalystRatingItem, AnalystTrackRecord, analyst_track_record
from factors.confidence import evaluate_cell_confidence
from psycopg import Connection
from truealpha_contracts.confidence import ContinuousConfidencePolicy, SourceConfidenceEvidence

from data_engine.datahub.canonical_issuer import require_canonical_issuer_id
from data_engine.datahub.fetch_key import FetchKey
from data_engine.datahub.production_topt.source_registrations import source_reliability_prior
from data_engine.datahub.production_topt.status_dimensions import statuses_without_raw_pointer

_CONFIDENCE_POLICY = ContinuousConfidencePolicy()

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
    "parse_rating_summary",
)

log = logging.getLogger(__name__)

_INSERT_SQL = """
insert into mart.issuer_analyst_ratings (
    run_id, issuer_id, cutoff, consensus_rating, analysts_count, buy_count, hold_count, sell_count,
    confidence, reason_codes, extractor, availability_status, source_evidence_status, factor_validation_status
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
MAX_FAILURES_LOGGED = 20


@dataclass(frozen=True)
class FetchFailure:
    ticker: str
    error: str


@dataclass(frozen=True)
class TickerCapture:
    rows: int
    failure: FetchFailure | None = None


@dataclass(frozen=True)
class UniverseCapture:
    rows: int
    failures: tuple[FetchFailure, ...] = ()

    def lane_failure(self) -> str | None:
        if self.rows > 0 and len(self.failures) == self.rows:
            first = self.failures[0]
            return (
                f"analyst ratings fetch failed for {len(self.failures)} of {self.rows} tickers; "
                f"first error: {first.ticker}: {first.error}"
            )
        return None


def _safe_id(raw: str, fallback: str) -> str:
    return "".join(c if c.isalnum() or c in "._:/@+-" else "-" for c in raw).strip("-") or fallback


def _moomoo_rating_confidence(
    case_id: str,
    evidence_id: str,
    *,
    agreement: Decimal = Decimal("1"),
    sample_conformance: Decimal = Decimal("1"),
    reason: str = "reason:single_origin",
) -> Decimal:
    return evaluate_cell_confidence(
        case_id=case_id,
        sources=(
            SourceConfidenceEvidence(
                provider_id="origin:moomoo-ratings:v1",
                origin_group_id="lineage:moomoo",
                independence_weight=Decimal("1"),
                reliability_prior=source_reliability_prior("origin:moomoo-ratings:v1"),
                successful_outcome_mass=Decimal("0"),
                failed_outcome_mass=Decimal("0"),
                freshness=Decimal("1"),
                sample_conformance=sample_conformance,
                transport_integrity=Decimal("1"),
                evidence_ids=(evidence_id,),
                reason_codes=(reason,),
            ),
        ),
        agreement=agreement,
        semantic_mapping_quality=Decimal("1"),
        lineage_completeness=Decimal("1"),
        required_component_completeness=Decimal("1"),
        policy=_CONFIDENCE_POLICY,
    ).confidence


def _count_from_share(total: int, share: object) -> int:
    if share is None:
        return 0
    if isinstance(share, bool) or not isinstance(share, Real):
        raise ValueError(f"analyst share {share!r} is not a number")
    percent = float(share)
    if not 0 <= percent <= 100:
        raise ValueError(f"analyst share {percent} is outside 0 to 100 percent")
    return round(total * percent / 100)


def _is_empty(payload: object) -> bool:
    if payload is None:
        return True
    if isinstance(payload, pd.DataFrame):
        return payload.empty
    return isinstance(payload, (Mapping, list)) and not payload


def _whole_number(payload: Mapping[str, Any], key: str) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, Real) or not float(value).is_integer():
        raise ValueError(f"moomoo {key} {value!r} is not a whole number")
    return int(float(value))


def _optional_target(payload: Mapping[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        val = payload.get(key)
        if val is not None and not isinstance(val, bool) and isinstance(val, Real):
            try:
                dec = Decimal(str(val))
            except Exception:  # noqa: BLE001
                continue
            if dec.is_finite() and dec >= 0:
                return dec
    return None


def _consensus_confidence(company_id: str) -> Decimal:
    safe_company = _safe_id(company_id, "issuer")
    return _moomoo_rating_confidence(f"analyst-consensus:{safe_company}", f"consensus:{safe_company}")


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
    return {
        "issuer_id": company_id,
        "consensus_rating": Decimal(rating),
        "analysts_count": count,
        "buy_count": _count_from_share(count, payload.get("buy")),
        "hold_count": _count_from_share(count, payload.get("hold")),
        "sell_count": _count_from_share(count, payload.get("sell")),
        "confidence": _consensus_confidence(company_id),
        "availability_status": "available",
        "reason_codes": [],
        "target_high": _optional_target(payload, "target_high", "highest"),
        "target_low": _optional_target(payload, "target_low", "lowest"),
        "target_mean": _optional_target(payload, "target_mean", "average"),
    }


def _analyst_confidence(analyst_id: str, raw_sr: object) -> Decimal:
    sr_factor = Decimal("0.85")
    if isinstance(raw_sr, Real) and not isinstance(raw_sr, bool):
        clamped = max(0.0, min(100.0, float(raw_sr)))
        sr_factor = Decimal(str(round(0.70 + 0.30 * (clamped / 100.0), 4)))
    safe_analyst = _safe_id(analyst_id, "analyst")
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
    rec_date = r_item.get("recommendation_date_str")
    raw_rating = r_item.get("rating")
    if not isinstance(rec_date, str) or len(rec_date) < 10 or rec_date[:10] > cutoff_iso:
        return None
    if isinstance(raw_rating, bool) or not isinstance(raw_rating, Real) or not float(raw_rating).is_integer():
        return None
    rating_int = int(float(raw_rating))
    return (rec_date[:10], rating_int) if _RATING_MIN <= rating_int <= _RATING_MAX else None


def _latest_rating_point_before_cutoff(items: object, cutoff_iso: str) -> tuple[str, int] | None:
    if not isinstance(items, list):
        return None
    latest: tuple[str, int] | None = None
    for r_item in items:
        point = _valid_rating_point(r_item, cutoff_iso)
        if point is not None and (latest is None or point[0] >= latest[0]):
            latest = point
    return latest


def _latest_rating_before_cutoff(items: object, cutoff_iso: str) -> int | None:
    point = _latest_rating_point_before_cutoff(items, cutoff_iso)
    return point[1] if point is not None else None


def parse_rating_summary(payload: object, *, as_of: datetime) -> list[AnalystRatingItem]:
    if _is_empty(payload) or not isinstance(payload, Mapping):
        return []
    summaries = payload.get("analyst_rating_summary_list")
    if not isinstance(summaries, list):
        return []
    cutoff_iso = as_of.date().isoformat()
    out: list[AnalystRatingItem] = []
    for entry in summaries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("analyst_info"), Mapping):
            continue
        analyst_info = entry["analyst_info"]
        analyst_id = str(analyst_info.get("analyst_id") or analyst_info.get("analyst_name") or "").strip()
        point = _latest_rating_point_before_cutoff(entry.get("rating_item_list"), cutoff_iso) if analyst_id else None
        if point is not None:
            latest_date, latest_rating = point
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
    return {
        "issuer_id": company_id,
        "consensus_rating": None,
        "analysts_count": 0,
        "buy_count": 0,
        "hold_count": 0,
        "sell_count": 0,
        "confidence": Decimal("0"),
        "availability_status": "unavailable",
        "reason_codes": [f"fetch_error:{type(exc).__name__}"],
    }


def _research_payload(code: str, endpoint: str, request: Callable[[FetchKey], Any]) -> Any:
    from data_engine.sources import gateway
    from data_engine.sources.moomoo import canonical_moomoo_bytes

    key = FetchKey("moomoo", endpoint, f"symbol:{code.removeprefix('US.')}", gateway.utc_day())
    return json.loads(gateway.fetch_once(key, lambda identity: canonical_moomoo_bytes(request(identity))).decode())


def _fetch_rating_enrichment(
    ctx: Any, code: str, *, as_of: datetime
) -> tuple[list[AnalystRatingItem], bool, dict[date, Decimal]]:
    from data_engine.datahub.production_topt.moomoo_origin import parse_valuation_profit_growth
    from data_engine.sources.moomoo import get_rating_summary, get_valuation_detail

    summary_items: list[AnalystRatingItem] = []
    if hasattr(ctx, "get_research_rating_summary"):
        try:
            summary_payload = _research_payload(
                code,
                "get_research_rating_summary",
                lambda identity: get_rating_summary(
                    ctx, code, caller="capture_ticker_analyst_ratings", identity=identity
                ),
            )
            summary_items = parse_rating_summary(summary_payload, as_of=as_of)
        except Exception:  # noqa: BLE001
            summary_items = []
    has_valuation = False
    profit_growth: dict[date, Decimal] = {}
    if hasattr(ctx, "get_valuation_detail"):
        try:
            val_payload = _research_payload(
                code,
                "get_valuation_detail",
                lambda identity: get_valuation_detail(
                    ctx, code, valuation_type=1, caller="capture_ticker_analyst_ratings", identity=identity
                ),
            )
            has_valuation = isinstance(val_payload, Mapping) and bool(val_payload)
            if isinstance(val_payload, Mapping):
                profit_growth = parse_valuation_profit_growth(val_payload, cutoff=as_of.date())
        except Exception:  # noqa: BLE001
            has_valuation, profit_growth = False, {}
    return summary_items, has_valuation, profit_growth


def _enrich_consensus_row(
    consensus_row: dict[str, Any],
    *,
    company_id: str,
    summary_items: list[AnalystRatingItem],
    has_valuation: bool,
    profit_growth: Mapping[date, Decimal],
    as_of: datetime | None = None,
) -> None:
    if not (summary_items or has_valuation):
        return
    if summary_items:
        weighted = analyst_track_record(summary_items, entity_id=company_id, as_of=as_of or datetime.now(tz=UTC))
        if weighted.consensus_rating is not None:
            consensus_row["consensus_rating"] = weighted.consensus_rating
        targets = [item.target_price for item in summary_items if item.target_price is not None]
        if consensus_row.get("target_high") is None and targets:
            consensus_row["target_high"] = max(targets)
        if consensus_row.get("target_low") is None and targets:
            consensus_row["target_low"] = min(targets)
        if consensus_row.get("target_mean") is None and targets:
            consensus_row["target_mean"] = sum(targets, start=Decimal(0)) / Decimal(len(targets))
        consensus_row["rating_items"] = list(summary_items)
    consensus_row["confidence"] = _moomoo_rating_confidence(
        f"consensus:{company_id}",
        f"consensus:{company_id}",
        agreement=min(item.confidence for item in summary_items) if summary_items else Decimal("0.90"),
        sample_conformance=Decimal("0.90") if summary_items else Decimal("0.95"),
        reason="vendor_timestamp",
    )
    if summary_items and "vendor_timestamp" not in consensus_row["reason_codes"]:
        consensus_row["reason_codes"] = [*consensus_row["reason_codes"], "vendor_timestamp"]
    if profit_growth:
        consensus_row["profit_growth_by_period"] = {
            end.isoformat(): str(rate) for end, rate in sorted(profit_growth.items())
        }
        if "valuation_profit_growth" not in consensus_row["reason_codes"]:
            consensus_row["reason_codes"] = [*consensus_row["reason_codes"], "valuation_profit_growth"]


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
        failure = FetchFailure(ticker=ticker, error=f"{type(open_error).__name__}: {open_error}")
        return _fetch_error_row(company_id, open_error), failure
    code = f"US.{ticker}" if not ticker.startswith("US.") else ticker
    try:
        from data_engine.sources.moomoo import get_analyst_consensus

        payload = _research_payload(
            code,
            "get_research_analyst_consensus",
            lambda identity: get_analyst_consensus(
                ctx, code, caller="capture_ticker_analyst_ratings", identity=identity
            ),
        )
        consensus_row = _consensus_row(payload, company_id)
        summary_items, has_valuation, profit_growth = _fetch_rating_enrichment(ctx, code, as_of=as_of)
    except Exception as exc:
        failure = FetchFailure(ticker=ticker, error=f"{type(exc).__name__}: {exc}")
        log.error(
            "analyst consensus fetch failed for %s (%s): %s", ticker, company_id, failure.error, exc_info=log_traceback
        )
        return _fetch_error_row(company_id, exc), failure
    if consensus_row is None:
        if summary_items:
            rec = analyst_track_record(summary_items, entity_id=company_id, as_of=as_of)
            targets = [item.target_price for item in summary_items if item.target_price is not None]
            return {
                "issuer_id": company_id,
                "consensus_rating": rec.consensus_rating,
                "analysts_count": rec.ratings_count,
                "buy_count": rec.buy_count,
                "hold_count": rec.hold_count,
                "sell_count": rec.sell_count,
                "confidence": rec.result.confidence,
                "availability_status": "available" if rec.consensus_rating is not None else "unavailable",
                "reason_codes": [*rec.result.flags, "vendor_timestamp"],
                "target_high": max(targets) if targets else None,
                "target_low": min(targets) if targets else None,
                "target_mean": (sum(targets, start=Decimal(0)) / Decimal(len(targets))) if targets else None,
                "rating_items": list(summary_items),
            }, None
        return analyst_track_record([], entity_id=company_id, as_of=as_of), None
    _enrich_consensus_row(
        consensus_row,
        company_id=company_id,
        summary_items=summary_items,
        has_valuation=has_valuation,
        profit_growth=profit_growth,
        as_of=as_of,
    )
    return consensus_row, None


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
    as_of = cutoff or datetime.now(tz=UTC)
    row, failure = _ticker_row(
        ctx, ticker=ticker, company_id=company_id, as_of=as_of, open_error=open_error, log_traceback=log_traceback
    )
    rows = materialize_analyst_ratings(connection, run_id=run_id, cutoff=as_of, ratings_data=[row])
    return TickerCapture(rows=rows, failure=failure)


def _persist_analyst_rating_events(
    connection: Connection[Any],
    *,
    company_id: str,
    as_of: datetime,
    rating_items: Sequence[AnalystRatingItem],
    run_id: str,
) -> int:
    valid_items = [item for item in rating_items if isinstance(item, AnalystRatingItem) and 1 <= item.rating <= 5]
    if not valid_items:
        return 0
    tx_fn = getattr(connection, "transaction", None)
    try:
        with tx_fn() if callable(tx_fn) else nullcontext():
            for item in valid_items:
                raw_rec = getattr(item, "recommendation_at", None) or getattr(item, "recommendation_date", None)
                if isinstance(raw_rec, date) and not isinstance(raw_rec, datetime):
                    rec_at = datetime(raw_rec.year, raw_rec.month, raw_rec.day, tzinfo=UTC)
                elif isinstance(raw_rec, datetime):
                    rec_at = raw_rec if raw_rec.tzinfo is not None else raw_rec.replace(tzinfo=UTC)
                else:
                    rec_at = as_of
                connection.execute(
                    "insert into staging.kg_entities (id, entity_type, display_name) values (%s, 'company', %s), (%s, 'analyst', %s) on conflict (id) do nothing",
                    (company_id, company_id, item.analyst_id, item.analyst_id),
                )
                connection.execute(
                    """
                    insert into staging.analyst_rating_events (
                        analyst_id, company_id, recommendation_at, transaction_time,
                        rating, target_price, confidence, raw_ref
                    )
                    select %s, %s, least(%s::timestamptz, %s::timestamptz, clock_timestamp()),
                           least(%s::timestamptz, clock_timestamp()), %s, %s, %s, %s
                    where to_regclass('staging.analyst_rating_events') is not null
                    on conflict (analyst_id, company_id, recommendation_at, transaction_time) do nothing
                    """,
                    (
                        item.analyst_id,
                        company_id,
                        rec_at,
                        as_of,
                        as_of,
                        int(item.rating),
                        item.target_price,
                        item.confidence if Decimal("0") <= item.confidence <= Decimal("1") else Decimal("0.8"),
                        f"moomoo:rating_summary:{run_id}:{company_id}:{item.analyst_id}",
                    ),
                )
        return len(valid_items)
    except Exception:  # noqa: BLE001
        return 0


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
    as_of = cutoff or datetime.now(tz=UTC)
    source_evidence, factor_validation = statuses_without_raw_pointer()
    source_status, val_status = source_evidence.value, factor_validation.value
    for item in ratings_data:
        rating_items: Sequence[AnalystRatingItem] = ()
        if isinstance(item, AnalystTrackRecord):
            issuer_id = item.entity_id
            extractor = "origin:moomoo:v1"
            consensus_rating, analysts_count, buy_count, hold_count, sell_count, confidence, reason_codes, avail = (
                _unpack_track_record(item)
            )
        else:
            issuer_id = str(item["issuer_id"])
            extractor = str(item.get("extractor", "origin:moomoo:v1"))
            if "ratings" in item:
                rating_items = item["ratings"]
                consensus_rating, analysts_count, buy_count, hold_count, sell_count, confidence, reason_codes, avail = (
                    _unpack_track_record(analyst_track_record(rating_items, entity_id=issuer_id, as_of=as_of))
                )
            else:
                if isinstance(item.get("rating_items"), Sequence):
                    rating_items = item["rating_items"]
                raw_c = item.get("consensus_rating")
                consensus_rating = Decimal(str(raw_c)) if raw_c is not None else None
                analysts_count = int(item.get("analysts_count", 0))
                buy_count = int(item.get("buy_count", 0))
                hold_count = int(item.get("hold_count", 0))
                sell_count = int(item.get("sell_count", 0))
                raw_conf = item.get("confidence")
                confidence = (
                    Decimal(str(raw_conf))
                    if raw_conf is not None
                    else (Decimal("0.8") if consensus_rating is not None else Decimal("0"))
                )
                reason_codes = list(item.get("reason_codes", []))
                avail = str(
                    item.get("availability_status", "available" if consensus_rating is not None else "unavailable")
                )

        canonical_id = require_canonical_issuer_id(issuer_id)
        connection.execute(
            _INSERT_SQL,
            (
                run_id,
                canonical_id,
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
        if rating_items:
            _persist_analyst_rating_events(
                connection, company_id=canonical_id, as_of=as_of, rating_items=rating_items, run_id=run_id
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
