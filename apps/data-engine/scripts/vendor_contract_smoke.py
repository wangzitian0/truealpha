"""Live vendor contract smoke against Yahoo, Twelve Data, U.S. Treasury, and moomoo."""

from __future__ import annotations

import argparse
import dataclasses
import json
import urllib.parse
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import data_engine.db as db
from data_engine.config import settings
from data_engine.sources import gateway

_FAILURES: list[str] = []
_RESULTS: list[tuple[str, str, bool, str]] = []
DEFAULT_VENDORS = ("yahoo", "twelvedata", "us-treasury", "moomoo")


def _check(name: str, ok: bool, detail: str = "", *, vendor: str | None = None) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    resolved_vendor = vendor or (name.split(":", 1)[0].strip().lower() or "unknown")
    _RESULTS.append((resolved_vendor, name, ok, detail))
    if not ok:
        _FAILURES.append(name)


def _skip(name: str, detail: str) -> None:
    print(f"SKIP  {name} — {detail}")


def _try(name: str, fn):
    try:
        return True, fn()
    except Exception as error:  # noqa: BLE001
        _check(name, False, f"{type(error).__name__}: {error}")
        return False, None


def _json_or_none(body: bytes):
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


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
    ok, res = _try("yahoo: chart endpoint reachable", lambda: fetch_daily_chart("AAPL", end=today))
    if not ok:
        return
    _body, bars = res
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


def check_twelve_data(*, skip_unconfigured: bool = False) -> None:
    key = settings.twelve_data_api_key
    if not key:
        if skip_unconfigured:
            _skip("twelvedata: TWELVE_DATA_API_KEY provisioned", "checks skipped — key unset")
            return
        _check("twelvedata: TWELVE_DATA_API_KEY provisioned", False, "checks skipped — provision the key")
        return
    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
    status, body = _twelve("eod", {"symbol": "AAPL", "date": str(tomorrow)}, key)
    _check("twelvedata: /eod no-data date returns an HTTP error status", status >= 400, f"status {status}")
    payload = _json_or_none(body)
    _check(
        "twelvedata: /eod error body is JSON with a code",
        payload is not None and "code" in payload,
        str(payload)[:80] if payload is not None else body[:60].decode(errors="replace"),
    )
    start = datetime.now(UTC).date() - timedelta(days=12)
    status, body = _twelve(
        "time_series",
        {"symbol": "AAPL", "interval": "1day", "start_date": str(start), "end_date": str(tomorrow), "outputsize": "12"},
        key,
    )
    ok_rows = ok_bar = False
    detail = bar_detail = f"status {status}"
    if status == 200 and (parsed := _json_or_none(body)) is not None:
        rows = parsed.get("values", [])
        ok_rows = bool(rows) and all("datetime" in r and isinstance(r.get("close"), str) for r in rows)
        ok_bar = (
            bool(rows)
            and all(isinstance(r.get(k), str) for r in rows for k in ("open", "high", "low"))
            and all(r.get("volume") in (None, "") or isinstance(r.get("volume"), str) for r in rows)
        )
        detail = bar_detail = f"{len(rows)} rows"
    elif status == 200:
        detail = bar_detail = "body not JSON"
    _check("twelvedata: time_series rows carry datetime + string close", ok_rows, detail)
    _check("twelvedata: time_series rows carry the open/high/low bar as strings (volume optional)", ok_bar, bar_detail)
    weekday = datetime.now(UTC).date() - timedelta(days=1)
    while weekday.weekday() >= 5:
        weekday -= timedelta(days=1)
    status, body = _twelve("eod", {"symbol": "AAPL", "date": str(weekday)}, key)
    ok_close, detail = False, f"status {status}"
    if status == 200 and (payload := _json_or_none(body)) is not None:
        ok_close = isinstance(payload.get("close"), str) and payload.get("datetime") == str(weekday)
        detail = f"{payload.get('datetime')} close {payload.get('close')}"
    elif status == 200:
        detail = "body not JSON"
    _check("twelvedata: /eod settled date returns a string close", ok_close, detail)


def check_us_treasury() -> None:
    import data_engine.datahub.treasury_rates as treasury_rates
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


def check_moomoo(*, skip_unconfigured: bool = False) -> None:
    from data_engine.datahub.capture_core.moomoo_origin import (
        BALANCE_SHEET_FIELDS,
        INCOME_FIELDS,
        INCOME_STATEMENT,
        OpenDClient,
        canonical_bytes,
        parse_settled_close,
        period_end_of,
    )
    from data_engine.datahub.fetch_key import FetchKey

    if not settings.moomoo_opend_host or not settings.moomoo_opend_port:
        if skip_unconfigured:
            _skip("moomoo: OPEND coordinates configured", "checks skipped — MOOMOO_OPEND_HOST/PORT unset")
            return
        _check("moomoo: OPEND coordinates configured", False, "checks skipped — set MOOMOO_OPEND_HOST/PORT")
        return
    client = OpenDClient()
    today = datetime.now(UTC).date()
    ok, bars = _try(
        "moomoo: OpenD reachable and K-line answers",
        lambda: client.history_kline(
            "US.AAPL",
            start=today - timedelta(days=14),
            end=today - timedelta(days=1),
            identity=FetchKey("moomoo", "history_kline", "symbol:US.AAPL", gateway.utc_day()),
        ),
    )
    if not ok:
        return
    _check("moomoo: OpenD reachable and K-line answers", True, f"{len(bars)} bars")
    _check(
        "moomoo: daily bars are stamped with a midnight session date and a float close",
        bool(bars)
        and all(str(b.get("time_key", "")).endswith(" 00:00:00") and isinstance(b.get("close"), float) for b in bars),
    )
    ok_q, quote = _try(
        "moomoo: the window parses to a settled close",
        lambda: parse_settled_close(canonical_bytes(bars), partition=today - timedelta(days=1)),
    )
    if ok_q:
        _check("moomoo: the window parses to a settled close", quote is not None)
    ok_inc, income = _try(
        "moomoo: income statement answers",
        lambda: client.financial_statements(
            "US.AAPL",
            statement_type=INCOME_STATEMENT,
            identity=FetchKey("moomoo", "financial_statements:income", "symbol:US.AAPL", gateway.utc_day()),
        ),
    )
    if not ok_inc:
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


def record_results_to_db(results: Sequence[tuple[str, str, bool, str]], connection: Any = None) -> int:
    """Persist per-vendor contract smoke pass/fail summaries to `staging.ingestion_health_log`."""
    grouped: dict[str, list[tuple[str, bool]]] = {}
    for vendor, check_name, ok, _detail in results:
        grouped.setdefault(vendor, []).append((check_name, ok))
    if not grouped:
        return 0

    rows = [
        (
            "vendor_contract_smoke",
            f"vendor:{vendor}:ok",
            0 if any(not ok for _, ok in items) else 1,
            json.dumps(
                {
                    "passed": sum(1 for _, ok in items if ok),
                    "failed": sum(1 for _, ok in items if not ok),
                    "failures": [name for name, ok in items if not ok],
                },
                sort_keys=True,
            ),
        )
        for vendor, items in sorted(grouped.items())
    ]

    sql = "insert into staging.ingestion_health_log (source, metric, value, note) values (%s, %s, %s, %s)"
    if connection is not None:
        for row in rows:
            connection.execute(sql, row)
        return len(rows)

    with db.connect() as owned:
        for row in rows:
            owned.execute(sql, row)
        owned.commit()
    return len(rows)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live vendor contract smoke against upstream APIs.")
    parser.add_argument(
        "--vendors",
        default=",".join(DEFAULT_VENDORS),
        help="Comma-separated vendors to check (yahoo,twelvedata,us-treasury,moomoo).",
    )
    parser.add_argument(
        "--skip-unconfigured",
        action="store_true",
        help="Skip vendors whose credentials/host settings are unset instead of failing.",
    )
    parser.add_argument(
        "--record-db",
        action="store_true",
        help="Persist per-vendor summary metrics into staging.ingestion_health_log.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    _FAILURES.clear()
    _RESULTS.clear()

    runners: dict[str, Callable[[], None]] = {
        "yahoo": check_yahoo,
        "twelvedata": lambda: check_twelve_data(skip_unconfigured=args.skip_unconfigured),
        "us-treasury": check_us_treasury,
        "moomoo": lambda: check_moomoo(skip_unconfigured=args.skip_unconfigured),
    }
    selected = [v.strip().lower() for v in args.vendors.split(",") if v.strip()]
    unknown = [v for v in selected if v not in runners]
    if unknown:
        raise SystemExit(f"unknown vendor(s): {', '.join(unknown)}")

    for vendor in selected:
        runners[vendor]()

    if args.record_db and _RESULTS:
        record_results_to_db(_RESULTS)

    if _FAILURES:
        print(f"\n{len(_FAILURES)} contract assumption(s) drifted: {', '.join(_FAILURES)}")
        return 1
    print("\nvendor contract holds")
    return 0


if __name__ == "__main__":
    with gateway.script_run("vendor_contract_smoke"):
        raise SystemExit(main())
