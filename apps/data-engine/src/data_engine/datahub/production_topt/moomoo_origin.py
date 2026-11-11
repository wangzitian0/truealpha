"""moomoo OpenD price and financial-fact corroboration origins."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from truealpha_contracts.models import DataSource

from data_engine.config import settings
from data_engine.datahub.fetch_key import FetchKey
from data_engine.datahub.production_topt.corroboration_audit import cached_corroboration_fetch
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
    AsOfAxis,
    knowable_instant,
)
from data_engine.sources import gateway

KLINE_ORIGIN = MOOMOO_KLINE_ORIGIN
KLINE_PARSER_VERSION = MOOMOO_KLINE_PARSER_VERSION
KLINE_MAPPING_VERSION = MOOMOO_KLINE_MAPPING_VERSION
KLINE_VALUE_KEY = MOOMOO_KLINE_VALUE_KEY
FINANCIALS_ORIGIN = MOOMOO_FINANCIALS_ORIGIN
FINANCIALS_PARSER_VERSION = MOOMOO_FINANCIALS_PARSER_VERSION
FINANCIALS_MAPPING_VERSION = MOOMOO_FINANCIALS_MAPPING_VERSION

CALLER = "moomoo_origin"
_KLINE_LOOKBACK_DAYS = 14
_KLINE_MAX_COUNT = 30
_PERIOD_END_ZONE = ZoneInfo("Asia/Shanghai")
_ANNUAL_FINANCIAL_TYPE = 7
INCOME_STATEMENT, BALANCE_SHEET = 1, 2
INCOME_FIELDS: Mapping[str, int] = {
    "revenue": 8001,
    "gross_profit": 8004,
    "net_income": 8037,
    "eps_basic": 8047,
    "eps_diluted": 8048,
}
BALANCE_SHEET_FIELDS: Mapping[str, int] = {"total_assets": 8001}
_SESSION_STAMP_LENGTH = len("2026-07-29 00:00:00")
_MIDNIGHT = " 00:00:00"


class NotASessionCloseError(ValueError):
    pass


class MoomooClient(Protocol):
    def history_kline(self, code: str, *, start: date, end: date, identity: FetchKey) -> list[dict[str, Any]]: ...

    def financial_statements(self, code: str, *, statement_type: int, identity: FetchKey) -> Mapping[str, Any]: ...


class OpenDClient:
    def history_kline(self, code: str, *, start: date, end: date, identity: FetchKey) -> list[dict[str, Any]]:
        from data_engine.jsonable import to_jsonable
        from data_engine.sources import moomoo

        with moomoo.connect() as ctx:
            bars, _page_req_key = moomoo.get_history_kline(
                ctx,
                code,
                start=start.isoformat(),
                end=end.isoformat(),
                max_count=_KLINE_MAX_COUNT,
                caller=CALLER,
                identity=identity,
            )
        records = to_jsonable(bars)
        return records if isinstance(records, list) else []

    def financial_statements(self, code: str, *, statement_type: int, identity: FetchKey) -> Mapping[str, Any]:
        from data_engine.jsonable import to_jsonable
        from data_engine.sources import moomoo

        with moomoo.connect() as ctx:
            statements = moomoo.get_financials_statements(
                ctx, code, statement_type=statement_type, caller=CALLER, identity=identity
            )
        normalized = to_jsonable(statements)
        return normalized if isinstance(normalized, Mapping) else {}


def moomoo_code(symbol: str) -> str:
    return f"US.{symbol}"


def canonical_bytes(payload: Any) -> bytes:
    from data_engine.sources.moomoo import canonical_moomoo_bytes

    return canonical_moomoo_bytes(payload)


def _recovered_decimal(value: int | float | str) -> Decimal:
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
    try:
        bars = json.loads(raw_bytes.decode())
    except (UnicodeDecodeError, ValueError) as error:
        raise NotASessionCloseError("moomoo K-line response is not JSON") from error
    if not isinstance(bars, list):
        raise NotASessionCloseError("moomoo K-line response is not a list of bars")
    settled: tuple[date, Mapping[str, Any]] | None = None
    for bar in bars:
        if not isinstance(bar, Mapping):
            continue
        as_of = _session_date(bar.get("time_key"))
        if as_of > partition:
            raise NotASessionCloseError(f"moomoo returned session {as_of}, after the {partition} partition")
        if settled is None or as_of > settled[0]:
            settled = (as_of, bar)
    if settled is None:
        return None
    as_of, bar = settled
    return MarketPriceQuote(
        raw_bytes=raw_bytes,
        close=_decimal(bar.get("close"), "close"),
        as_of=as_of,
        knowable_at=knowable_instant(AsOfAxis.SETTLED_SESSION, as_of),
        open=_optional_decimal(bar.get("open"), "open"),
        high=_optional_decimal(bar.get("high"), "high"),
        low=_optional_decimal(bar.get("low"), "low"),
        volume=_optional_decimal(bar.get("volume"), "volume"),
        currency=_bar_currency(bar),
    )


def _bar_currency(bar: Mapping[str, Any]) -> str:
    raw = bar.get("currency")
    return raw.strip() if isinstance(raw, str) and len(raw.strip()) == 3 else "USD"


def _optional_decimal(value: object, what: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return _decimal(value, what)


class MoomooKlineFetcher:
    def __init__(self, client: MoomooClient) -> None:
        self._client = client
        self._cache: dict[tuple[str, date], MarketPriceQuote | None] = {}

    def __call__(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
        return cached_corroboration_fetch(
            self._cache, (symbol, cutoff), KLINE_ORIGIN, symbol, lambda: self._fetch(symbol, cutoff)
        )

    def _fetch(self, symbol: str, cutoff: date) -> MarketPriceQuote | None:
        key = FetchKey(
            "moomoo", f"history_kline?lookback={_KLINE_LOOKBACK_DAYS}", f"symbol:{symbol}", cutoff.isoformat()
        )

        def fetch(identity: FetchKey) -> bytes:
            bars = self._client.history_kline(
                moomoo_code(symbol),
                start=cutoff - timedelta(days=_KLINE_LOOKBACK_DAYS),
                end=cutoff,
                identity=identity,
            )
            return canonical_bytes(bars)

        return parse_settled_close(gateway.fetch_once(key, fetch), partition=cutoff)


def moomoo_kline_origin() -> CorroboratingOrigin | None:
    if not settings.moomoo_kline_origin_enabled:
        return None
    _require_opend()
    return CorroboratingOrigin(
        origin=KLINE_ORIGIN,
        parser_version=KLINE_PARSER_VERSION,
        mapping_version=KLINE_MAPPING_VERSION,
        value_key=KLINE_VALUE_KEY,
        fetch=MoomooKlineFetcher(OpenDClient()),
        raw_source=DataSource.MOOMOO,
    )


def period_end_of(report: Mapping[str, Any]) -> date | None:
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
    try:
        payload = json.loads(raw_bytes.decode())
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    income: Mapping[str, Any] = payload["income"] if isinstance(payload.get("income"), Mapping) else {}
    balance: Mapping[str, Any] = payload["balance_sheet"] if isinstance(payload.get("balance_sheet"), Mapping) else {}
    valuation: Mapping[str, Any] = (
        payload["valuation_detail"] if isinstance(payload.get("valuation_detail"), Mapping) else {}
    )
    by_period: dict[date, dict[str, Decimal | None]] = {}
    for statement, fields, annual_only in (
        (income, INCOME_FIELDS, True),
        (balance, BALANCE_SHEET_FIELDS, False),
    ):
        for end, values in annual_values_by_period_end(statement, fields, annual_only=annual_only).items():
            if end <= cutoff:
                by_period.setdefault(end, {}).update(values)
    for end, growth_rate in parse_valuation_profit_growth(valuation, cutoff=cutoff).items():
        by_period.setdefault(end, {})["consensus_profit_growth"] = growth_rate
    if not by_period:
        return None
    fiscal_years = [
        end for end, values in by_period.items() if any(values.get(name) is not None for name in INCOME_FIELDS)
    ]
    newest = max(fiscal_years) if fiscal_years else max(by_period)
    currency, standards = _statement_meta(income) or _statement_meta(balance) or (None, None)
    headline_values = {name: by_period[newest].get(name) for name in (*INCOME_FIELDS, *BALANCE_SHEET_FIELDS)}
    if "consensus_profit_growth" in by_period[newest]:
        headline_values["consensus_profit_growth"] = by_period[newest]["consensus_profit_growth"]
    return FinancialFactAssertion(
        raw_bytes=raw_bytes,
        period_end=newest,
        values=headline_values,
        by_period_end=by_period,
        knowable_at=knowable_instant(AsOfAxis.SETTLED_SESSION, cutoff),
        currency=currency,
        accounting_standards=standards,
    )


def _statement_meta(statement: Mapping[str, Any]) -> tuple[str | None, str | None] | None:
    reports = statement.get("report_list")
    if not isinstance(reports, list):
        return None
    for report in reports:
        if isinstance(report, Mapping) and report.get("financial_type") == _ANNUAL_FINANCIAL_TYPE:
            currency = report.get("currency_code")
            standards = report.get("accounting_standards")
            return (
                currency if isinstance(currency, str) and len(currency) == 3 else None,
                standards if isinstance(standards, str) and standards else None,
            )
    return None


class MoomooFinancialsFetcher:
    def __init__(self, client: MoomooClient) -> None:
        self._client = client
        self._cache: dict[tuple[str, date], FinancialFactAssertion | None] = {}

    def __call__(self, symbol: str, cutoff: date) -> FinancialFactAssertion | None:
        return cached_corroboration_fetch(
            self._cache, (symbol, cutoff), FINANCIALS_ORIGIN, symbol, lambda: self._fetch(symbol, cutoff)
        )

    def _fetch(self, symbol: str, cutoff: date) -> FinancialFactAssertion | None:
        code = moomoo_code(symbol)
        income = self._statement(code, symbol, "income", INCOME_STATEMENT)
        balance = self._statement(code, symbol, "balance_sheet", BALANCE_SHEET)
        return parse_annual_financials(canonical_bytes({"income": income, "balance_sheet": balance}), cutoff=cutoff)

    def _statement(self, code: str, symbol: str, label: str, statement_type: int) -> Any:
        key = FetchKey("moomoo", f"financial_statements:{label}", f"symbol:{symbol}", gateway.utc_day())

        def fetch(identity: FetchKey) -> bytes:
            return canonical_bytes(
                self._client.financial_statements(code, statement_type=statement_type, identity=identity)
            )

        return json.loads(gateway.fetch_once(key, fetch).decode())


def moomoo_financials_origin() -> FinancialFactCorroboratingOrigin | None:
    if not settings.moomoo_financials_origin_enabled:
        return None
    _require_opend()
    return FinancialFactCorroboratingOrigin(
        origin=FINANCIALS_ORIGIN,
        parser_version=FINANCIALS_PARSER_VERSION,
        mapping_version=FINANCIALS_MAPPING_VERSION,
        fetch=MoomooFinancialsFetcher(OpenDClient()),
        raw_source=DataSource.MOOMOO,
    )


def parse_valuation_profit_growth(valuation: Mapping[str, Any], *, cutoff: date) -> dict[date, Decimal]:
    pg = valuation.get("profit_growth_rate")
    if not isinstance(pg, Mapping):
        return {}
    rows = pg.get("profit_data")
    if not isinstance(rows, list):
        return {}
    out: dict[date, Decimal] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        period_str = row.get("period_str")
        fq = row.get("financial_quarter")
        if not ((isinstance(period_str, str) and period_str.endswith("/FY")) or fq == 4):
            continue
        report_date_str = row.get("report_date_str")
        if not isinstance(report_date_str, str) or len(report_date_str) < 10:
            continue
        try:
            end_date = date.fromisoformat(report_date_str[:10])
        except ValueError:
            continue
        if end_date > cutoff or end_date in out:
            continue
        rate = _statement_value(row.get("profit_growth_rate"))
        if rate is not None:
            out[end_date] = rate
    return out


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
    "parse_valuation_profit_growth",
    "period_end_of",
)
