"""Live vendor contract smoke: assert the properties the parsers RELY on, against
the real vendors, for a few API credits (#539: shrink the test-reality gap).

The cassette suite (`tests/production_topt/test_real_vendor_bytes.py`) pins the
parsers to bytes the vendors really sent; this script checks the other direction —
that the vendors still send bytes of that shape. Both exist because two premise
bugs shipped green on 2026-08-14: #557 (the live `/eod` answers a no-data date
with an HTTP 400 *status*, not an error body on 200 — and `urlopen` raises) and
#535 (a `time_series` bar for the partition date is still forming during a
session). Every check below is one of those load-bearing assumptions, named.

Cost: 1 Yahoo request + 3 Twelve Data credits (of the 800/day budget) + 2 moomoo
calls (gated and ledgered like any other; AAPL is already inside the K-line quota
window once the governed universe has been captured).
Exit 0 = contract holds; exit 1 = a named assumption drifted. Intended for a
cron/scheduled run and after any vendor incident. A missing TWELVE_DATA_API_KEY is
recorded as a FAILED check (a monitor with no credentials is a monitoring gap, not
a skip).

Usage:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/vendor_contract_smoke.py
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from data_engine.config import settings

_FAILURES: list[str] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        _FAILURES.append(name)


def _twelve(path: str, params: dict[str, str], api_key: str) -> tuple[int, bytes]:
    """Status + body, treating an HTTP error status as an answer, exactly as the
    origin's `_get` does post-#557 — the body IS the vendor's reply."""
    query = urllib.parse.urlencode({**params, "apikey": api_key})
    try:
        with urllib.request.urlopen(f"https://api.twelvedata.com/{path}?{query}", timeout=20) as r:  # noqa: S310
            return r.status, bytes(r.read())
    except urllib.error.HTTPError as error:
        with error:
            return error.code, bytes(error.read())
    except OSError as error:
        # DNS, timeout, refused, TLS — transport failure is itself a drifted
        # assumption for a monitor: report it as a failed check, never crash past
        # the summary (Copilot review on #569).
        return 0, f'{{"transport_error": "{type(error).__name__}"}}'.encode()


def check_yahoo() -> None:
    from data_engine.sources.yahoo import fetch_daily_chart

    today = datetime.now(UTC).date()
    try:
        body, bars = fetch_daily_chart("AAPL", end=today)
    except Exception as error:  # noqa: BLE001 - a monitor reports, it does not crash
        _check("yahoo: chart endpoint reachable", False, f"{type(error).__name__}: {error}")
        return
    _check("yahoo: chart endpoint reachable", True)
    _check("yahoo: chart returns parseable bars", len(bars) >= 1, f"{len(bars)} bars")
    if bars:
        newest = max(bars, key=lambda b: b.date)
        _check(
            "yahoo: closes decode as Decimal (never binary float)",
            isinstance(newest.close, Decimal),
            f"{newest.date} close {newest.close}",
        )
        _check("yahoo: newest bar is recent", today - newest.date <= timedelta(days=7), str(newest.date))
        # Parser v10 asserts the whole bar; a chart that stopped carrying one of these
        # would land four honest nulls per cell and every bar field single-origin.
        _check(
            "yahoo: newest bar carries open/high/low as Decimal and an integer volume",
            all(isinstance(value, Decimal) for value in (newest.open, newest.high, newest.low))
            and isinstance(newest.volume, int),
            f"open {newest.open} high {newest.high} low {newest.low} volume {newest.volume}",
        )


def check_twelve_data() -> None:
    key = settings.twelve_data_api_key
    if not key:
        _check("twelvedata: TWELVE_DATA_API_KEY provisioned", False, "checks skipped — provision the key")
        return

    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
    status, body = _twelve("eod", {"symbol": "AAPL", "date": str(tomorrow)}, key)
    # The #557 assumption: a no-data date answers with an HTTP error status AND a
    # JSON body. If the vendor ever switches to 200-with-error-body, the origin
    # still works — but this check tells us the contract moved.
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
    ok_rows = False
    ok_bar = False
    detail = f"status {status}"
    bar_detail = detail
    if status == 200:
        try:
            rows = json.loads(body).get("values", [])
            ok_rows = bool(rows) and all("datetime" in r and isinstance(r.get("close"), str) for r in rows)
            detail = f"{len(rows)} rows, close is a string"
            # The twelve-data v3 assumption: the bar the origin attaches to a settled
            # close lives on these rows as strings. `volume` is documented optional and
            # `twelve_data_origin._decimal_or_absent` reads an absent or empty volume as
            # an absent assertion, so only its type is checked here, never its presence.
            ok_bar = (
                bool(rows)
                and all(isinstance(r.get(key), str) for r in rows for key in ("open", "high", "low"))
                and all(r.get("volume") in (None, "") or isinstance(r.get("volume"), str) for r in rows)
            )
            with_volume = sum(1 for r in rows if isinstance(r.get("volume"), str) and r.get("volume") != "")
            bar_detail = f"{len(rows)} rows carry open/high/low as strings, volume on {with_volume}"
        except json.JSONDecodeError:
            detail = bar_detail = "body not JSON"
    _check("twelvedata: time_series rows carry datetime + string close", ok_rows, detail)
    _check("twelvedata: time_series rows carry the open/high/low bar as strings (volume optional)", ok_bar, bar_detail)

    # A settled session must resolve through /eod directly (the primary path).
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


def check_moomoo() -> None:
    """The two shapes the moomoo origins rely on (`datahub.production_topt.moomoo_origin`):
    a daily K-line bar stamped with a midnight session date and a float close, and an
    annual statement row (`financial_type == 7`) carrying the calibrated field ids with
    an epoch period end. Needs the OpenD coordinates; unconfigured is a FAILED check,
    as for a missing Twelve Data key — a monitor without access is a gap, not a skip."""
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
        bars = client.history_kline("US.AAPL", start=today - timedelta(days=14), end=today - timedelta(days=1))
    except Exception as error:  # noqa: BLE001 - a monitor reports, it does not crash
        _check("moomoo: OpenD reachable and K-line answers", False, f"{type(error).__name__}: {error}")
        return
    _check("moomoo: OpenD reachable and K-line answers", True, f"{len(bars)} bars")
    _check(
        "moomoo: daily bars are stamped with a midnight session date and a float close",
        bool(bars)
        and all(str(b.get("time_key", "")).endswith(" 00:00:00") and isinstance(b.get("close"), float) for b in bars),
        str(bars[-1].get("time_key")) if bars else "no bars",
    )
    try:
        quote = parse_settled_close(canonical_bytes(bars), partition=today - timedelta(days=1))
        _check(
            "moomoo: the window parses to a settled close",
            quote is not None,
            f"{quote.as_of} {quote.close}" if quote else "",
        )
    except Exception as error:  # noqa: BLE001
        _check("moomoo: the window parses to a settled close", False, f"{type(error).__name__}: {error}")
    try:
        income = client.financial_statements("US.AAPL", statement_type=INCOME_STATEMENT)
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
            f"{newest.get('date_time_str')} -> {end}",
        )
    _check("moomoo: balance-sheet field map declared", "total_assets" in BALANCE_SHEET_FIELDS)


def main() -> int:
    check_yahoo()
    check_twelve_data()
    check_moomoo()
    if _FAILURES:
        print(f"\n{len(_FAILURES)} contract assumption(s) drifted: {', '.join(_FAILURES)}")
        return 1
    print("\nvendor contract holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
