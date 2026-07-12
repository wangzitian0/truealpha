"""Normalize the bounded moomoo domains used by the capture scope."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from factors.shared import entity_resolution as er
from truealpha_contracts import canonical_sha256

from data_engine import raw_store
from data_engine.normalizers import lineage

MAPPING_VERSION = "moomoo-fundamentals:1"
CONFIDENCE = Decimal("0.8")
ACTION_CONFIDENCE = Decimal("0.9")


def _payload(conn, raw_fetch_id: int) -> tuple[Any, datetime, str]:
    row = conn.execute("select fetched_at from raw.fetches where id = %s", (raw_fetch_id,)).fetchone()
    if row is None:
        raise LookupError(f"raw.fetches:{raw_fetch_id} does not exist")
    return json.loads(raw_store.get_payload(conn, raw_fetch_id)), row[0], raw_store.raw_ref(raw_fetch_id)


def _timestamp(value: Any) -> datetime:
    numeric = int(value)
    while numeric > 10_000_000_000:
        numeric //= 1000
    return datetime.fromtimestamp(numeric, tz=UTC)


def normalize_consensus(conn, *, raw_fetch_id: int, issuer_id: str, currency: str = "USD") -> tuple[int, ...]:
    payload, fetched_at, raw_ref = _payload(conn, raw_fetch_id)
    if not isinstance(payload, dict):
        raise ValueError("moomoo analyst_consensus payload must be an object")
    required = ("average", "highest", "lowest", "rating", "total", "update_time")
    if any(payload.get(field) is None for field in required):
        return ()
    knowable_at = max(_timestamp(payload["update_time"]), fetched_at - timedelta(microseconds=1))
    # A vendor timestamp can predate our first observation, but this historical
    # snapshot was not defensibly available to us before capture. The later of
    # the two is the anti-backfill boundary.
    knowable_at = max(knowable_at, fetched_at)
    forecast_period = f"rolling-12m:{knowable_at.date().isoformat()}"
    row = conn.execute(
        """
        insert into staging.forecast_facts
            (issuer_id, metric, forecast_period, estimate, estimate_low,
             estimate_high, currency, valid_time, transaction_time, recorded_at,
             confidence, source, source_metric, raw_ref, mapping_version)
        values (%s, 'target_price_12m', %s, %s, %s, %s, %s,
                daterange(%s::date, (%s::date + 1), '[)'), %s, %s, %s,
                'moomoo', 'analyst_consensus.average', %s, %s)
        on conflict do nothing returning id
        """,
        (
            issuer_id,
            forecast_period,
            Decimal(str(payload["average"])),
            Decimal(str(payload["lowest"])),
            Decimal(str(payload["highest"])),
            currency,
            knowable_at.date(),
            knowable_at.date(),
            knowable_at,
            datetime.now(UTC),
            CONFIDENCE,
            raw_ref,
            MAPPING_VERSION,
        ),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            select id from staging.forecast_facts
            where issuer_id = %s and metric = 'target_price_12m' and forecast_period = %s
              and transaction_time = %s and source = 'moomoo' and raw_ref = %s
              and mapping_version = %s
            """,
            (issuer_id, forecast_period, knowable_at, raw_ref, MAPPING_VERSION),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"could not persist or recover consensus for {issuer_id}")
    lineage.link(
        conn,
        table="forecast_facts",
        record_id=row[0],
        raw_ref=raw_ref,
        mapping_version=MAPPING_VERSION,
    )
    return (row[0],)


def normalize_ratings(conn, *, raw_fetch_id: int, issuer_id: str, currency: str = "USD") -> tuple[int, ...]:
    payload, fetched_at, raw_ref = _payload(conn, raw_fetch_id)
    if not isinstance(payload, dict):
        raise ValueError("moomoo rating_summary payload must be an object")
    summaries = payload.get("analyst_rating_summary_list")
    if summaries is None:
        raise ValueError("moomoo rating_summary lost analyst_rating_summary_list")
    if not isinstance(summaries, list):
        raise ValueError("moomoo analyst_rating_summary_list must be a list")
    recorded_at = datetime.now(UTC)
    ids: list[int] = []
    for summary in summaries:
        if not isinstance(summary, dict) or not isinstance(summary.get("analyst_info"), dict):
            raise ValueError("moomoo rating summary lost analyst_info")
        analyst = summary["analyst_info"]
        uid = analyst.get("analyst_uid")
        name = analyst.get("analyst_name")
        items = summary.get("rating_item_list")
        if not uid or not name or not isinstance(items, list):
            raise ValueError("moomoo rating summary lost analyst identity/history")
        analyst_id = f"analyst:moomoo:{uid}"
        er.ensure_entity(conn, analyst_id, "analyst", str(name))
        ordered = sorted(
            (item for item in items if isinstance(item, dict) and item.get("recommendation_date") is not None),
            key=lambda item: int(item["recommendation_date"]),
        )
        prior_rating: int | None = None
        for item in ordered:
            if item.get("rating") is None or item.get("update_time") is None:
                raise ValueError("moomoo rating item lost rating/update_time")
            rating = int(item["rating"])
            recommendation_at = _timestamp(item["recommendation_date"])
            vendor_updated_at = _timestamp(item["update_time"])
            knowable_at = max(recommendation_at, vendor_updated_at, fetched_at)
            if prior_rating is None:
                action = "initial"
            elif rating > prior_rating:
                action = "upgrade"
            elif rating < prior_rating:
                action = "downgrade"
            else:
                action = "reiterate"
            row = conn.execute(
                """
                insert into staging.analyst_rating_events
                    (analyst_id, company_id, recommendation_at, transaction_time,
                     vendor_updated_at, recorded_at, rating, target_price, currency,
                     source_url, confidence, raw_ref, action, source, mapping_version)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, 'moomoo', %s)
                on conflict do nothing returning id
                """,
                (
                    analyst_id,
                    issuer_id,
                    recommendation_at,
                    knowable_at,
                    vendor_updated_at,
                    recorded_at,
                    rating,
                    Decimal(str(item["target_price"])) if item.get("target_price") is not None else None,
                    currency,
                    item.get("rating_url"),
                    CONFIDENCE,
                    raw_ref,
                    action,
                    MAPPING_VERSION,
                ),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    select id from staging.analyst_rating_events
                    where analyst_id = %s and company_id = %s
                      and recommendation_at = %s and transaction_time = %s
                    """,
                    (analyst_id, issuer_id, recommendation_at, knowable_at),
                ).fetchone()
            if row is None:
                raise RuntimeError(f"could not persist or recover rating {analyst_id}/{issuer_id}")
            ids.append(row[0])
            lineage.link(
                conn,
                table="analyst_rating_events",
                record_id=row[0],
                raw_ref=raw_ref,
                mapping_version=MAPPING_VERSION,
            )
            prior_rating = rating
    return tuple(sorted(set(ids)))


_SEGMENT_TYPES = {4: "geography", 8: "product"}


def normalize_segments(conn, *, raw_fetch_id: int, issuer_id: str) -> tuple[int, ...]:
    payload, fetched_at, raw_ref = _payload(conn, raw_fetch_id)
    if not isinstance(payload, dict):
        raise ValueError("moomoo revenue_breakdown payload must be an object")
    period = payload.get("period")
    currency = payload.get("currency_code")
    breakdowns = payload.get("breakdown_list")
    screens = payload.get("screen_date_list")
    if not period or not currency or not isinstance(breakdowns, list) or not isinstance(screens, list):
        raise ValueError("moomoo revenue_breakdown lost period/currency/breakdown/screen dates")
    period_screen = next((screen for screen in screens if screen.get("period_text") == period), None)
    if period_screen is None or period_screen.get("date") is None:
        raise ValueError("moomoo revenue_breakdown lost the selected period date")
    period_end = _timestamp(period_screen["date"]).date()
    recorded_at = datetime.now(UTC)
    ids: list[int] = []
    for breakdown in breakdowns:
        if not isinstance(breakdown, dict) or not isinstance(breakdown.get("item_list"), list):
            raise ValueError("moomoo revenue breakdown item lost item_list")
        segment_type = _SEGMENT_TYPES.get(int(breakdown.get("type", -1)), f"vendor:{breakdown.get('type')}")
        for item in breakdown["item_list"]:
            if not isinstance(item, dict) or item.get("name") is None or item.get("main_oper_income") is None:
                raise ValueError("moomoo segment lost name/main_oper_income")
            row = conn.execute(
                """
                insert into staging.segment_facts
                    (issuer_id, segment_type, segment_name, metric, fiscal_period,
                     value, unit, valid_time, transaction_time, recorded_at,
                     confidence, source, raw_ref, mapping_version, taxonomy_version)
                values (%s, %s, %s, 'revenue', %s, %s, %s,
                        daterange(%s::date, (%s::date + 1), '[)'), %s, %s, %s,
                        'moomoo', %s, %s, 'moomoo-segments:1')
                on conflict do nothing returning id
                """,
                (
                    issuer_id,
                    segment_type,
                    str(item["name"]),
                    str(period),
                    Decimal(str(item["main_oper_income"])),
                    str(currency),
                    period_end,
                    period_end,
                    fetched_at,
                    recorded_at,
                    CONFIDENCE,
                    raw_ref,
                    MAPPING_VERSION,
                ),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    select id from staging.segment_facts
                    where issuer_id = %s and segment_type = %s and segment_name = %s
                      and metric = 'revenue' and fiscal_period = %s
                      and transaction_time = %s and source = 'moomoo'
                      and raw_ref = %s and mapping_version = %s
                    """,
                    (
                        issuer_id,
                        segment_type,
                        str(item["name"]),
                        str(period),
                        fetched_at,
                        raw_ref,
                        MAPPING_VERSION,
                    ),
                ).fetchone()
            if row is None:
                raise RuntimeError(f"could not persist or recover segment {issuer_id}/{item['name']}")
            ids.append(row[0])
            lineage.link(
                conn,
                table="segment_facts",
                record_id=row[0],
                raw_ref=raw_ref,
                mapping_version=MAPPING_VERSION,
            )
    return tuple(sorted(set(ids)))


_DIVIDEND_RE = re.compile(r"Cash Dividend:\s*([0-9.]+)\s+([A-Z]{3})\s+Per Share", re.IGNORECASE)


def _us_date(value: str | None) -> date | None:
    return None if not value else datetime.strptime(value, "%m/%d/%Y").date()


def normalize_dividends(
    conn,
    *,
    raw_fetch_id: int,
    instrument_id: str,
    listing_id: str,
) -> tuple[int, ...]:
    payload, fetched_at, raw_ref = _payload(conn, raw_fetch_id)
    if not isinstance(payload, dict) or not isinstance(payload.get("dividend_list"), list):
        raise ValueError("moomoo dividends payload lost dividend_list")
    recorded_at = datetime.now(UTC)
    ids: list[int] = []
    for item in payload["dividend_list"]:
        if not isinstance(item, dict):
            raise ValueError("moomoo dividend item must be an object")
        match = _DIVIDEND_RE.search(str(item.get("statement") or ""))
        ex_date = _us_date(item.get("ex_date"))
        published = _us_date(item.get("pub_date"))
        if match is None or ex_date is None or published is None:
            raise ValueError("moomoo cash dividend lost amount/currency/pub/ex date")
        amount = Decimal(match.group(1))
        currency = match.group(2).upper()
        knowable_at = datetime.combine(published, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
        if knowable_at > fetched_at:
            knowable_at = fetched_at
        event_id = "action:" + canonical_sha256(
            {
                "instrument_id": instrument_id,
                "source": "moomoo",
                "action_type": "cash_dividend",
                "ex_date": ex_date.isoformat(),
                "amount": str(amount),
                "currency": currency,
            }
        )
        row = conn.execute(
            """
            insert into staging.corporate_actions
                (action_event_id, instrument_id, listing_id, action_type,
                 declaration_at, ex_date, effective_date, record_date, pay_date,
                 cash_amount, currency, transaction_time, recorded_at,
                 confidence, source, raw_ref, mapping_version)
            values (%s, %s, %s, 'cash_dividend', %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, 'moomoo', %s, %s)
            on conflict do nothing returning id
            """,
            (
                event_id,
                instrument_id,
                listing_id,
                knowable_at,
                ex_date,
                ex_date,
                _us_date(item.get("record_date")),
                _us_date(item.get("dividend_payable_date")),
                amount,
                currency,
                knowable_at,
                recorded_at,
                ACTION_CONFIDENCE,
                raw_ref,
                MAPPING_VERSION,
            ),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                select id from staging.corporate_actions
                where action_event_id = %s and transaction_time = %s
                  and source = 'moomoo' and raw_ref = %s and mapping_version = %s
                """,
                (event_id, knowable_at, raw_ref, MAPPING_VERSION),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"could not persist or recover moomoo dividend {event_id}")
        ids.append(row[0])
        lineage.link(
            conn,
            table="corporate_actions",
            record_id=row[0],
            raw_ref=raw_ref,
            mapping_version=MAPPING_VERSION,
        )
    return tuple(sorted(set(ids)))
