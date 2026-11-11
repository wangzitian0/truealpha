"""Live vendor contract smoke against Yahoo, Twelve Data, U.S. Treasury, and moomoo."""

from __future__ import annotations

import dataclasses
import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from data_engine.config import settings
from data_engine.sources import gateway

_FAILURES: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        _FAILURES.append(name)


def _twelve(path: str, params: dict[str, str], api_key: str) -> tuple[int, bytes]:
    from data_engine.datahub.fetch_key import FetchKey

    query = urllib.parse.urlencode({**params, "apikey": api_key})
    url = f"https://api.twelvedata.com/{path}?{query}"
    identity = FetchKey.for_request(
        "twelvedata",
        f"contract_smoke:{path}",
        params,
        entity_param="symbol",
        as_of_params=("date", "end_date"),
        default_as_of=gateway.utc_day(),
    )
    try:
        status, body = gateway.urlopen(
            "twelvedata", path, url, identity=identity, caller="vendor_contract_smoke._twelve", timeout=20
        )
        return status or 0, body
    except OSError as error:
        return 0, f'{{"transport_error": "{type(error).__name__}"}}'.encode()


def check_yahoo() -> None:
    from data_engine.sources.yahoo import fetch_daily_chart

    today = datetime.now(UTC).date()
    try:
        _body, bars = fetch_daily_chart("AAPL", end=today)
    except Exception as error:  # noqa: BLE001
        _check("yahoo: chart endpoint reachable", False, f"{type(error).__name__}: {error}")
        return
    _check("yahoo: chart endpoint reachable", True)
    _check("yahoo: chart returns parseable bars", len(bars) >= 1, f"{len(bars)} bars")
    if bars:
        newest = max(bars, key=lambda b: b.date)
        _check(
            "yahoo: closes decode as Decimal (never binary float)", isinstance(newest.close, Decimal), f"{newest.close}"
        )
        _check("yahoo: newest bar is recent", today - newest.date <= timedelta(days=7), str(newest.date))
        _check(
            "yahoo: newest bar carries open/high/low as Decimal and an integer volume",
            all(isinstance(v, Decimal) for v in (newest.open, newest.high, newest.low))
            and isinstance(newest.volume, int),
        )


def check_twelve_data() -> None:
    key = settings.twelve_data_api_key
    if not key:
        _check("twelvedata: TWELVE_DATA_API_KEY provisioned", False, "checks skipped — provision the key")
        return

    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
    status, body = _twelve("eod", {"symbol": "AAPL", "date": str(tomorrow)}, key)
    _check("twelvedata: /eod no-data date returns an HTTP error status", status >= 400, f"status {status}")
    try:
        payload = json.loads(body)
        _check("twelvedata: /eod error body is JSON with a code", "code" in payload, str(payload)[:80])
    except json.JSONDecodeError:
        _check("twelvedata: /eod error body is JSON with a code", False, body[:60].decode(errors="replace"))

    start = datetime.now(UTC).date() - timedelta(days=12)
    status, body = _twelve(
        "time_series",
        {"symbol": "AAPL", "interval": "1day", "start_date": str(start), "end_date": str(tomorrow), "outputsize": "12"},
        key,
    )
    ok_rows = ok_bar = False
    detail = bar_detail = f"status {status}"
    if status == 200:
        try:
            rows = json.loads(body).get("values", [])
            ok_rows = bool(rows) and all("datetime" in r and isinstance(r.get("close"), str) for r in rows)
            ok_bar = (
                bool(rows)
                and all(isinstance(r.get(k), str) for r in rows for k in ("open", "high", "low"))
                and all(r.get("volume") in (None, "") or isinstance(r.get("volume"), str) for r in rows)
            )
            detail = f"{len(rows)} rows"
            bar_detail = detail
        except json.JSONDecodeError:
            detail = bar_detail = "body not JSON"
    _check("twelvedata: time_series rows carry datetime + string close", ok_rows, detail)
    _check("twelvedata: time_series rows carry the open/high/low bar as strings (volume optional)", ok_bar, bar_detail)

    weekday = datetime.now(UTC).date() - timedelta(days=1)
    while weekday.weekday() >= 5:
        weekday -= timedelta(days=1)
    status, body = _twelve("eod", {"symbol": "AAPL", "date": str(weekday)}, key)
    ok_close = False
    detail = f"status {status}"
    if status == 200:
        try:
            payload = json.loads(body)
            ok_close = isinstance(payload.get("close"), str) and payload.get("datetime") == str(weekday)
            detail = f"{payload.get('datetime')} close {payload.get('close')}"
        except json.JSONDecodeError:
            detail = "body not JSON"
    _check("twelvedata: /eod settled date returns a string close", ok_close, detail)


def check_us_treasury() -> None:
    from data_engine.datahub import treasury_rates
    from data_engine.datahub.risk_free import MAX_RATE_LAG_DAYS

    today = datetime.now(UTC).date()
    for year in (today.year, today.year - 1):
        try:
            monitor_key = dataclasses.replace(treasury_rates.fetch_key_of(year, on=today), endpoint="contract_smoke")
            body = treasury_rates.fetch_year(year, monitor_key)
            parsed = treasury_rates.parse_par_yield_csv(body, year=year)
        except Exception as error:  # noqa: BLE001
            _check(f"us-treasury: the {year} file is reachable and parses", False, f"{type(error).__name__}: {error}")
            return
        rows = sum(len(items) for items in parsed.values())
        _check(f"us-treasury: the {year} file is reachable and parses", True, f"{len(body)} bytes, {rows} values")
        if rows:
            break
    else:
        _check("us-treasury: a file with at least one observation", False, "two files without a row")
        return
    for series_id, observations in parsed.items():
        if not observations:
            _check(f"us-treasury: {series_id} has an observation", False)
            continue
        newest = max(observations, key=lambda item: item.observation_date)
        age = (today - newest.observation_date).days
        _check(
            f"us-treasury: the newest {series_id} observation is within the lag limit of the resolver",
            age <= MAX_RATE_LAG_DAYS,
            f"{age} days old",
        )
        _check(
            f"us-treasury: {series_id} values decode as Decimal fractions between 0 and 1",
            all(isinstance(item.value, Decimal) and 0 <= item.value < 1 for item in observations),
        )


def check_moomoo() -> None:
    from data_engine.datahub.fetch_key import FetchKey
    from data_engine.datahub.production_topt.moomoo_origin import (
        BALANCE_SHEET_FIELDS,
        INCOME_FIELDS,
        INCOME_STATEMENT,
        OpenDClient,
        canonical_bytes,
        parse_settled_close,
        period_end_of,
    )

    if not settings.moomoo_opend_host or not settings.moomoo_opend_port:
        _check("moomoo: OPEND coordinates configured", False, "checks skipped — set MOOMOO_OPEND_HOST/PORT")
        return
    client = OpenDClient()
    today = datetime.now(UTC).date()
    try:
        bars = client.history_kline(
            "US.AAPL",
            start=today - timedelta(days=14),
            end=today - timedelta(days=1),
            identity=FetchKey("moomoo", "history_kline", "symbol:US.AAPL", gateway.utc_day()),
        )
    except Exception as error:  # noqa: BLE001
        _check("moomoo: OpenD reachable and K-line answers", False, f"{type(error).__name__}: {error}")
        return
    _check("moomoo: OpenD reachable and K-line answers", True, f"{len(bars)} bars")
    _check(
        "moomoo: daily bars are stamped with a midnight session date and a float close",
        bool(bars)
        and all(str(b.get("time_key", "")).endswith(" 00:00:00") and isinstance(b.get("close"), float) for b in bars),
    )
    try:
        quote = parse_settled_close(canonical_bytes(bars), partition=today - timedelta(days=1))
        _check("moomoo: the window parses to a settled close", quote is not None)
    except Exception as error:  # noqa: BLE001
        _check("moomoo: the window parses to a settled close", False, f"{type(error).__name__}: {error}")
    try:
        income = client.financial_statements(
            "US.AAPL",
            statement_type=INCOME_STATEMENT,
            identity=FetchKey("moomoo", "financial_statements:income", "symbol:US.AAPL", gateway.utc_day()),
        )
    except Exception as error:  # noqa: BLE001
        _check("moomoo: income statement answers", False, f"{type(error).__name__}: {error}")
        return
    annual = [r for r in income.get("report_list", []) if r.get("financial_type") == 7]
    _check("moomoo: income statement carries annual rows", bool(annual), f"{len(annual)} annual rows")
    if annual:
        newest = annual[0]
        ids = {item.get("field_id") for item in newest.get("item_list", [])}
        missing = sorted(name for name, field_id in INCOME_FIELDS.items() if field_id not in ids)
        _check("moomoo: calibrated income field ids are present", not missing, f"missing {missing}" if missing else "")
        end = period_end_of(newest)
        _check(
            "moomoo: period end is an epoch that lands on a month end in Asia/Shanghai",
            end is not None and (end + timedelta(days=1)).day == 1,
        )
    _check("moomoo: balance-sheet field map declared", "total_assets" in BALANCE_SHEET_FIELDS)


def main() -> int:
    check_yahoo()
    check_twelve_data()
    check_us_treasury()
    check_moomoo()
    if _FAILURES:
        print(f"\n{len(_FAILURES)} contract assumption(s) drifted: {', '.join(_FAILURES)}")
        return 1
    print("\nvendor contract holds")
    return 0


if __name__ == "__main__":
    with gateway.script_run("vendor_contract_smoke"):
        raise SystemExit(main())
