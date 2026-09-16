"""moomoo OpenD as a governed origin (init.md rule 15, A3 decision 1): the THIRD
market-price origin and the SECOND financial-fact origin.

Why this exists: OpenD has been live on the VPS since 2026-07 and the data engine's
containers can reach it, yet `staging.api_call_ledger` holds zero moomoo rows and every
financial-fact cell is single-origin (class B, falsified only by the plausibility oracle).
The owner's standard needs three price origins and a second origin for fundamentals, so
that revenue / net income can reach HIGH when two vendors agree. Both origins here are
corroborators: the primary (Yahoo, SEC company-facts) still selects the served value, the
fusion engine (#343) reconciles under a declared tolerance and abstains on conflict, and a
moomoo outage leaves cells honestly single-origin rather than failing the tick.

## The client is injectable

Every OpenD call goes through `data_engine.sources.moomoo._call` — gate, throttle, call,
record — so the monthly backstop and `staging.api_call_ledger` see it (rule 6). The origin
talks to that module through the `MoomooClient` protocol; the deployed `OpenDClient`
dials OpenD, the tests use a cassette-backed double. No test needs OpenD.

## What the bytes are

The SDK speaks protobuf over TCP and hands back DataFrames and dicts, never a response
body. The landed bytes are therefore the SDK's decoded answer in canonical JSON — the
closest byte-for-byte artifact the vendor path exposes — and the parser identity names
that rendering (`moomoo-kline-parser:v1` / `moomoo-financials-parser:v1`).

## The quantity, per origin

* K-line (`moomoo-kline`): the UNADJUSTED regular-session close of the settled session
  at/before the price cutoff, requested as a bounded daily window ending on the cutoff.
  The cutoff is already the last settled session (#637), so the in-progress bar is never
  inside the window; a bar stamped with an instant rather than a session date, or a bar
  after the cutoff, is refused (#535) instead of corroborating the wrong quantity.
* Financials (`moomoo-financials`): the annual (`financial_type == 7`) income-statement
  flows and the balance-sheet instants at every reported period end, keyed by period
  end on the same axis the SEC adapter uses. moomoo stamps a period end as 00:00 Asia/Shanghai — its UTC rendering
  (`date_time_str`) is the day BEFORE the calendar period end — so the epoch is converted
  in that zone. Field ids are numeric and undocumented client-side (samples/README.md);
  the mapping below was established by equality against the SEC XBRL facts for the four
  captured issuers across FY2023-FY2025 (`tests/production_topt/test_moomoo_origin.py`
  keeps measuring it).

moomoo publishes no filing date, so a financials assertion is knowable when it is served:
`knowable_at` is the cutoff, and only periods that ENDED at/before the cutoff are
asserted. The fusion aligns on the primary's filed-and-knowable period, so a vendor
restatement or an early earnings release can corroborate only a period the primary
already has.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from truealpha_contracts.models import DataSource

from data_engine.config import settings
from data_engine.datahub.production_topt.market_price_adapter import CorroboratingOrigin, MarketPriceQuote
from data_engine.datahub.production_topt.sec_financial_adapter import (
    FinancialFactAssertion,
    FinancialFactCorroboratingOrigin,
)
from data_engine.datahub.production_topt.source_registrations import (
    MOOMOO_FINANCIALS_MAPPING_VERSION,
    MOOMOO_FINANCIALS_ORIGIN,
    MOOMOO_FINANCIALS_PARSER_VERSION,
    MOOMOO_KLINE_MAPPING_VERSION,
    MOOMOO_KLINE_ORIGIN,
    MOOMOO_KLINE_PARSER_VERSION,
    MOOMOO_KLINE_VALUE_KEY,
)

KLINE_ORIGIN = MOOMOO_KLINE_ORIGIN
KLINE_PARSER_VERSION = MOOMOO_KLINE_PARSER_VERSION
KLINE_MAPPING_VERSION = MOOMOO_KLINE_MAPPING_VERSION
KLINE_VALUE_KEY = MOOMOO_KLINE_VALUE_KEY
FINANCIALS_ORIGIN = MOOMOO_FINANCIALS_ORIGIN
FINANCIALS_PARSER_VERSION = MOOMOO_FINANCIALS_PARSER_VERSION
FINANCIALS_MAPPING_VERSION = MOOMOO_FINANCIALS_MAPPING_VERSION

CALLER = "moomoo_origin"
# A no-SLA consolidated-tape close and a vendor-normalized statement: below the primary's
# grade, above the floor a stale fallback earns (`graded_price_confidence`).
KLINE_CONFIDENCE = Decimal("0.80")
FINANCIALS_CONFIDENCE = Decimal("0.75")
# Two weeks of calendar days covers any holiday cluster before the cutoff so the settled
# session is inside the window; `max_count` bounds the answer to the same span.
_KLINE_LOOKBACK_DAYS = 14
_KLINE_MAX_COUNT = 30
# moomoo stamps a fiscal period end at midnight in this zone (OpenD's home time).
_PERIOD_END_ZONE = ZoneInfo("Asia/Shanghai")
# `FinancialType` values in a `report_list` row: 1-4 = single quarters, 7 = annual.
_ANNUAL_FINANCIAL_TYPE = 7
# `FinancialStatementsType` (proto enum ints; the SDK exposes no enum class).
INCOME_STATEMENT, BALANCE_SHEET = 1, 2
# Statement field ids -> payload keys. Established by equality against SEC company-facts
# (DDOG, DUOL, NICE, SHOP; FY2023-FY2025): 8001 total revenue, 8004 gross profit, 8037 net
# income (ProfitLoss — NICE FY2024 442,588,000 = us-gaap:ProfitLoss, 0.70% above
# NetIncomeLoss after minority interest), 8047 basic EPS, 8048 diluted EPS; balance sheet
# 8001 total assets. Every other id is left alone until it is calibrated the same way.
INCOME_FIELDS: Mapping[str, int] = {
    "revenue": 8001,
    "gross_profit": 8004,
    "net_income": 8037,
    "eps_basic": 8047,
    "eps_diluted": 8048,
}
BALANCE_SHEET_FIELDS: Mapping[str, int] = {"total_assets": 8001}
# A settled daily bar is stamped with its session date at midnight; an intraday bar or a
# quote carries a real instant.
_SESSION_STAMP_LENGTH = len("2026-07-29 00:00:00")
_MIDNIGHT = " 00:00:00"


class NotASessionCloseError(ValueError):
    """moomoo answered with a quantity that is not a settled regular-session close."""


class MoomooClient(Protocol):
    """The two OpenD calls the origins need, as JSON-safe structures.

    Implemented by `OpenDClient` for the deployed path and by a cassette double in tests.
    Either way the answer is what the SDK decoded, already normalized to plain lists and
    dicts, so the origin can land it as canonical bytes and parse it from those bytes —
    the same bytes a replay would read back from `raw.fetches`.
    """

    def history_kline(self, code: str, *, start: date, end: date) -> list[dict[str, Any]]: ...

    def financial_statements(self, code: str, *, statement_type: int) -> Mapping[str, Any]: ...


class OpenDClient:
    """`MoomooClient` over a live OpenD, one gated connection per call.

    A connection per call is deliberate: `OpenQuoteContext` runs a socket thread that
    nothing in the adapter's lifetime would otherwise close, and the ledger's 8-per-30 s
    pacing dwarfs the handshake. Imported lazily so the origin module carries no SDK
    dependency until an environment enables it.
    """

    def history_kline(self, code: str, *, start: date, end: date) -> list[dict[str, Any]]:
        from data_engine.jsonable import to_jsonable
        from data_engine.sources import moomoo

        with moomoo.connect() as ctx:
            bars, _page_req_key = moomoo.get_history_kline(
                ctx, code, start=start.isoformat(), end=end.isoformat(), max_count=_KLINE_MAX_COUNT, caller=CALLER
            )
        records = to_jsonable(bars)
        return records if isinstance(records, list) else []

    def financial_statements(self, code: str, *, statement_type: int) -> Mapping[str, Any]:
        from data_engine.jsonable import to_jsonable
        from data_engine.sources import moomoo

        with moomoo.connect() as ctx:
            statements = moomoo.get_financials_statements(ctx, code, statement_type=statement_type, caller=CALLER)
        normalized = to_jsonable(statements)
        return normalized if isinstance(normalized, Mapping) else {}


def moomoo_code(symbol: str) -> str:
    """moomoo's code for a US listing: the canonical ticker under the `US.` market prefix."""
    return f"US.{symbol}"


def canonical_bytes(payload: Any) -> bytes:
    """The landed rendering of an SDK answer: canonical JSON, key-sorted, no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


# --- market price: the settled daily close --------------------------------------------


def _recovered_decimal(value: int | float | str) -> Decimal:
    """The SDK decodes every figure as a binary float; `str()` is the shortest base-10
    rendering that round-trips (the recovery the SEC parser applies to company-facts),
    and an integral figure drops the float's `.0` so a dollar amount lands as `3427158000`
    rather than `3427158000.0` — the same value, the rendering the primary writes."""
    recovered = Decimal(str(value))
    if recovered.is_finite() and recovered == recovered.to_integral_value():
        return recovered.quantize(Decimal(1))
    return recovered


def _decimal(value: object, what: str) -> Decimal:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float | str):
        raise NotASessionCloseError(f"moomoo bar carries no {what}")
    try:
        return _recovered_decimal(value)
    except InvalidOperation as error:
        raise NotASessionCloseError(f"moomoo {what} {value!r} is not a number") from error


def _session_date(stamp: object) -> date:
    if not isinstance(stamp, str) or len(stamp) != _SESSION_STAMP_LENGTH or not stamp.endswith(_MIDNIGHT):
        raise NotASessionCloseError(f"moomoo stamped {stamp!r} with an instant, not a session date")
    try:
        return date.fromisoformat(stamp[:10])
    except ValueError as error:
        raise NotASessionCloseError(f"moomoo session stamp {stamp!r} is not a date") from error


def parse_settled_close(raw_bytes: bytes, *, partition: date) -> MarketPriceQuote | None:
    """The newest daily regular-session close from a session at/before `partition`.

    `partition` is the LAST SETTLED session (`last_settled_session_date`), so a bar dated
    on it has closed; a bar after it is look-ahead and refused. Returns None when the
    window holds no bar (a listing moomoo does not carry, an empty answer).
    """
    try:
        bars = json.loads(raw_bytes.decode())
    except (UnicodeDecodeError, ValueError) as error:
        raise NotASessionCloseError("moomoo K-line response is not JSON") from error
    if not isinstance(bars, list):
        raise NotASessionCloseError("moomoo K-line response is not a list of bars")
    settled: tuple[date, Decimal] | None = None
    for bar in bars:
        if not isinstance(bar, Mapping):
            continue
        as_of = _session_date(bar.get("time_key"))
        if as_of > partition:
            raise NotASessionCloseError(f"moomoo returned session {as_of}, after the {partition} partition")
        if settled is None or as_of > settled[0]:
            settled = (as_of, _decimal(bar.get("close"), "close"))
    if settled is None:
        return None
    as_of, close = settled
    return MarketPriceQuote(
        raw_bytes=raw_bytes,
        close=close,
        as_of=as_of,
        knowable_at=datetime.combine(as_of, datetime.min.time(), tzinfo=UTC),
    )


class MoomooKlineFetcher:
    """`MarketPriceFetcher` over moomoo's daily K-line, memoized per (symbol, cutoff).

    Every failure — OpenD down, the monthly backstop, a refused quantity — is absorbed
    into "absent": a third origin that errors must leave the cell where it was, never
    fail the primary capture. The gate and the ledger row happen inside the client.
    """

    def __init__(self, client: MoomooClient) -> None:
        self._client = client
        self._cache: dict[tuple[str, date], MarketPriceQuote | None] = {}

    def __call__(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
        key = (symbol, cutoff)
        if key in self._cache:
            return self._cache[key]
        try:
            quote = self._fetch(symbol, cutoff)
        except Exception:  # noqa: BLE001 - a corroborating origin that errors is simply absent
            quote = None
        self._cache[key] = quote
        return quote

    def _fetch(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
        bars = self._client.history_kline(
            moomoo_code(symbol), start=cutoff - timedelta(days=_KLINE_LOOKBACK_DAYS), end=cutoff
        )
        return parse_settled_close(canonical_bytes(bars), partition=cutoff)


def moomoo_kline_origin() -> CorroboratingOrigin | None:
    """The configured third price origin, or None when this environment has not enabled it.

    Enabled but unconfigured OpenD coordinates raise here, at route build, rather than
    surfacing as a per-cell timeout: the flag says "use moomoo", and dialling a guess is
    what `sources.moomoo.connect` refuses to do.
    """
    if not settings.moomoo_kline_origin_enabled:
        return None
    _require_opend()
    return CorroboratingOrigin(
        origin=KLINE_ORIGIN,
        parser_version=KLINE_PARSER_VERSION,
        mapping_version=KLINE_MAPPING_VERSION,
        value_key=KLINE_VALUE_KEY,
        confidence=KLINE_CONFIDENCE,
        fetch=MoomooKlineFetcher(OpenDClient()),
        raw_source=DataSource.MOOMOO,
    )


# --- financial facts: annual statements on the SEC period axis --------------------------


def period_end_of(report: Mapping[str, Any]) -> date | None:
    """The fiscal period end a `report_list` row describes, on the SEC `period_end` axis.

    The epoch is midnight Asia/Shanghai on the period-end date; rendering it in UTC
    (`date_time_str`) lands the day before, which is why the string is not used.
    """
    stamp = report.get("date_time")
    if isinstance(stamp, bool) or not isinstance(stamp, int | float):
        return None
    return datetime.fromtimestamp(stamp, tz=_PERIOD_END_ZONE).date()


def _statement_value(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        recovered = _recovered_decimal(value)
    except InvalidOperation:
        return None
    return recovered if recovered.is_finite() else None


def annual_values_by_period_end(
    statement: Mapping[str, Any], fields: Mapping[str, int], *, annual_only: bool = True
) -> dict[date, dict[str, Decimal | None]]:
    """Figures of one statement keyed by period end, one dict of named values each.

    Income-statement figures are FLOWS: only `financial_type == 7` (annual) rows qualify,
    because a Q4 row is a quarter, never a year. Balance-sheet figures are INSTANTS, so
    with `annual_only=False` every reported period end qualifies — the primary's
    `total_assets` is the newest instant company-facts carries, a 10-Q's as often as a
    10-K's, and the same instant is what corroborates it. A period end that appears
    twice (the FY row and the Q4 row share one date) keeps the row seen first.
    """
    reports = statement.get("report_list")
    if not isinstance(reports, list):
        return {}
    by_field_id = {field_id: name for name, field_id in fields.items()}
    out: dict[date, dict[str, Decimal | None]] = {}
    for report in reports:
        if not isinstance(report, Mapping):
            continue
        if annual_only and report.get("financial_type") != _ANNUAL_FINANCIAL_TYPE:
            continue
        end = period_end_of(report)
        if end is None or end in out:
            continue
        values: dict[str, Decimal | None] = dict.fromkeys(fields)
        for item in report.get("item_list") or ():
            if not isinstance(item, Mapping):
                continue
            field_id = item.get("field_id")
            name = by_field_id.get(field_id) if isinstance(field_id, int) else None
            if name is not None:
                values[name] = _statement_value(item.get("data"))
        out[end] = values
    return out


def parse_annual_financials(raw_bytes: bytes, *, cutoff: date) -> FinancialFactAssertion | None:
    """What moomoo asserts for periods that ended at/before `cutoff`: annual income flows,
    and balance-sheet instants at every reported period end.

    `raw_bytes` is the canonical rendering of `{"income": <statement>, "balance_sheet":
    <statement>}`; either statement may be absent (`None`), in which case its fields are
    None for every period. The headline (`period_end`, `values`) is the newest ANNUAL
    income period — a coherent fiscal-year snapshot — and falls back to the newest
    instant when the income statement is absent. Returns None when nothing qualifies.
    """
    try:
        payload = json.loads(raw_bytes.decode())
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    income: Mapping[str, Any] = payload["income"] if isinstance(payload.get("income"), Mapping) else {}
    balance: Mapping[str, Any] = payload["balance_sheet"] if isinstance(payload.get("balance_sheet"), Mapping) else {}
    by_period: dict[date, dict[str, Decimal | None]] = {}
    for statement, fields, annual_only in (
        (income, INCOME_FIELDS, True),
        (balance, BALANCE_SHEET_FIELDS, False),
    ):
        for end, values in annual_values_by_period_end(statement, fields, annual_only=annual_only).items():
            if end > cutoff:
                continue
            by_period.setdefault(end, {}).update(values)
    if not by_period:
        return None
    fiscal_years = [
        end for end, values in by_period.items() if any(values.get(name) is not None for name in INCOME_FIELDS)
    ]
    newest = max(fiscal_years) if fiscal_years else max(by_period)
    currency, standards = _statement_meta(income) or _statement_meta(balance) or ("USD", None)
    return FinancialFactAssertion(
        raw_bytes=raw_bytes,
        period_end=newest,
        values={name: by_period[newest].get(name) for name in (*INCOME_FIELDS, *BALANCE_SHEET_FIELDS)},
        by_period_end=by_period,
        # Served now, asserted for periods that have ended: knowable at the cutoff.
        knowable_at=datetime.combine(cutoff, datetime.min.time(), tzinfo=UTC),
        currency=currency,
        accounting_standards=standards,
    )


def _statement_meta(statement: Mapping[str, Any]) -> tuple[str, str | None] | None:
    reports = statement.get("report_list")
    if not isinstance(reports, list):
        return None
    for report in reports:
        if isinstance(report, Mapping) and report.get("financial_type") == _ANNUAL_FINANCIAL_TYPE:
            currency = report.get("currency_code")
            standards = report.get("accounting_standards")
            return (
                currency if isinstance(currency, str) and len(currency) == 3 else "USD",
                standards if isinstance(standards, str) and standards else None,
            )
    return None


class MoomooFinancialsFetcher:
    """`FinancialFactCorroborator` over moomoo's income statement + balance sheet.

    Two gated calls per issuer per run (memoized), landed as ONE raw object so the
    corroborating observation dereferences to everything it was parsed from.
    """

    def __init__(self, client: MoomooClient) -> None:
        self._client = client
        self._cache: dict[tuple[str, date], FinancialFactAssertion | None] = {}

    def __call__(self, symbol: str, cutoff: date) -> FinancialFactAssertion | None:
        key = (symbol, cutoff)
        if key in self._cache:
            return self._cache[key]
        try:
            assertion = self._fetch(symbol, cutoff)
        except Exception:  # noqa: BLE001 - a corroborating origin that errors is simply absent
            assertion = None
        self._cache[key] = assertion
        return assertion

    def _fetch(self, symbol: str, cutoff: date) -> FinancialFactAssertion | None:
        code = moomoo_code(symbol)
        income = self._client.financial_statements(code, statement_type=INCOME_STATEMENT)
        balance = self._client.financial_statements(code, statement_type=BALANCE_SHEET)
        return parse_annual_financials(canonical_bytes({"income": income, "balance_sheet": balance}), cutoff=cutoff)


def moomoo_financials_origin() -> FinancialFactCorroboratingOrigin | None:
    """The configured second financial-fact origin, or None when not enabled here."""
    if not settings.moomoo_financials_origin_enabled:
        return None
    _require_opend()
    return FinancialFactCorroboratingOrigin(
        origin=FINANCIALS_ORIGIN,
        parser_version=FINANCIALS_PARSER_VERSION,
        mapping_version=FINANCIALS_MAPPING_VERSION,
        confidence=FINANCIALS_CONFIDENCE,
        fetch=MoomooFinancialsFetcher(OpenDClient()),
        raw_source=DataSource.MOOMOO,
    )


def _require_opend() -> None:
    if not settings.moomoo_opend_host or not settings.moomoo_opend_port:
        raise ValueError(
            "a moomoo origin is enabled but MOOMOO_OPEND_HOST / MOOMOO_OPEND_PORT are not configured; "
            "the origin refuses to dial a guess (sources.moomoo.connect)"
        )


__all__ = (
    "BALANCE_SHEET_FIELDS",
    "INCOME_FIELDS",
    "MoomooClient",
    "MoomooFinancialsFetcher",
    "MoomooKlineFetcher",
    "NotASessionCloseError",
    "OpenDClient",
    "annual_values_by_period_end",
    "canonical_bytes",
    "moomoo_code",
    "moomoo_financials_origin",
    "moomoo_kline_origin",
    "parse_annual_financials",
    "parse_settled_close",
    "period_end_of",
)
