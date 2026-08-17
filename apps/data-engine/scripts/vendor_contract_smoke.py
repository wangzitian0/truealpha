"""Live vendor contract smoke: assert the properties the parsers RELY on, against
the real vendors, for a few API credits (#539: shrink the test-reality gap).

The cassette suite (`tests/production_topt/test_real_vendor_bytes.py`) pins the
parsers to bytes the vendors really sent; this script checks the other direction —
that the vendors still send bytes of that shape. Both exist because two premise
bugs shipped green on 2026-08-14: #557 (the live `/eod` answers a no-data date
with an HTTP 400 *status*, not an error body on 200 — and `urlopen` raises) and
#535 (a `time_series` bar for the partition date is still forming during a
session). Every check below is one of those load-bearing assumptions, named.

Cost: 1 Yahoo request + 3 Twelve Data credits (of the 800/day budget).
Exit 0 = contract holds; exit 1 = a named assumption drifted. Intended for a
cron/scheduled run and after any vendor incident; requires TWELVE_DATA_API_KEY
for the Twelve Data checks (skips them, loudly, when absent).

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


def check_yahoo() -> None:
    from data_engine.sources.yahoo import fetch_daily_chart

    today = datetime.now(UTC).date()
    body, bars = fetch_daily_chart("AAPL", end=today)
    _check("yahoo: chart returns parseable bars", len(bars) >= 1, f"{len(bars)} bars")
    if bars:
        newest = max(bars, key=lambda b: b.date)
        _check(
            "yahoo: closes decode as Decimal (never binary float)",
            isinstance(newest.close, Decimal),
            f"{newest.date} close {newest.close}",
        )
        _check("yahoo: newest bar is recent", today - newest.date <= timedelta(days=7), str(newest.date))


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
    detail = f"status {status}"
    if status == 200:
        try:
            rows = json.loads(body).get("values", [])
            ok_rows = bool(rows) and all("datetime" in r and isinstance(r.get("close"), str) for r in rows)
            detail = f"{len(rows)} rows, close is a string"
        except json.JSONDecodeError:
            detail = "body not JSON"
    _check("twelvedata: time_series rows carry datetime + string close", ok_rows, detail)

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


def main() -> int:
    check_yahoo()
    check_twelve_data()
    if _FAILURES:
        print(f"\n{len(_FAILURES)} contract assumption(s) drifted: {', '.join(_FAILURES)}")
        return 1
    print("\nvendor contract holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
