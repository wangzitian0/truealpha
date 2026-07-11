#!/usr/bin/env python3
"""Capture the bounded public-source evidence set tracked by issue #14.

The selection is semantic, not broad: JPM (financial), ADM (traditional), NVDA
(split), META (symbol history), and PLUG (restatement). Existing sample companies
remain in the three-year price window. Files are immutable: a differing recapture
must use a new dated filename instead of overwriting committed evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, date, datetime
from hashlib import sha256
from html import unescape
from io import StringIO
from pathlib import Path

import httpx
from data_engine.sources.sec import COMPANY_FACTS_URL
from data_engine.sources.sec import client as sec_client
from data_engine.sources.yahoo import CHART_URL, USER_AGENT, fetch_daily_bars

SAMPLE_ROOT = Path(__file__).resolve().parents[1] / "samples"
CAPTURE_DATE = date(2026, 7, 12)
PRICE_SYMBOLS = ("DDOG", "DUOL", "NICE", "SHOP", "JPM", "ADM", "NVDA", "META", "PLUG")
PRICE_PERIOD_DAYS = {symbol: 3 * 366 for symbol in PRICE_SYMBOLS} | {"META": 5 * 366}
COMPANIES = {
    "JPM": 19617,
    "ADM": 7084,
    "NVDA": 1045810,
    "META": 1326801,
    "PLUG": 1093691,
}
SEC_ARTIFACTS = {
    "filings/JPM_10K_000162828026008131.html": (
        19617,
        "000162828026008131/jpm-20251231.htm",
    ),
    "filings/ADM_10K_000000708426000011.html": (
        7084,
        "000000708426000011/adm-20251231.htm",
    ),
    "filings/NVDA_8K_SPLIT_000104581024000144.html": (
        1045810,
        "000104581024000144/nvda-20240607.htm",
    ),
    "filings/NVDA_8K_GUIDANCE_000104581024000113.html": (
        1045810,
        "000104581024000113/q1fy25cfocommentary.htm",
    ),
    "filings/META_8K_SYMBOL_000132680122000070.html": (
        1326801,
        "000132680122000070/may312022-exhibit991.htm",
    ),
    "filings/PLUG_10K_000155837021007147.html": (
        1093691,
        "000155837021007147/plug-20201231x10k.htm",
    ),
    "filings/PLUG_10KA_000155837022003577.html": (
        1093691,
        "000155837022003577/plug-20201231x10ka.htm",
    ),
    "nport/QQQ_NPORT_000106783926000016.xml": (
        1067839,
        "000106783926000016/primary_doc.xml",
    ),
    "nport/ARKK_NPORT_000094040026012617.xml": (
        1579982,
        "000094040026012617/primary_doc.xml",
    ),
}
JPM_DIVIDEND_PATH = "events/JPM_DIVIDEND_20250519.json"
JPM_DIVIDEND_URL = "https://www.jpmorganchase.com/ir/news/2025/jpmc-declares-common-stock-dividend-5-19"
PUBLIC_ARTIFACTS = {
    "events/DDOG_RATING_CORROBORATION_20260712.html": "https://mboum.com/quotes/DDOG",
}


def _write_immutable(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise RuntimeError(f"refusing to overwrite changed evidence: {path}")
        print(f"unchanged {path.relative_to(SAMPLE_ROOT)}")
        return
    path.write_bytes(body)
    print(f"captured {path.relative_to(SAMPLE_ROOT)} ({len(body)} bytes)")


def capture_sec(*, resume: bool) -> None:
    with sec_client() as http:
        for ticker, cik in COMPANIES.items():
            path = SAMPLE_ROOT / "sec" / f"{ticker}_CIK{cik:010d}.json"
            if resume and path.exists():
                print(f"resume skip {path.relative_to(SAMPLE_ROOT)}")
                continue
            response = http.get(COMPANY_FACTS_URL.format(cik=cik))
            response.raise_for_status()
            _write_immutable(path, response.content)
        for relative_path, (cik, archive_path) in SEC_ARTIFACTS.items():
            if resume and (SAMPLE_ROOT / relative_path).exists():
                print(f"resume skip {relative_path}")
                continue
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{archive_path}"
            response = http.get(url)
            response.raise_for_status()
            _write_immutable(SAMPLE_ROOT / relative_path, response.content)


def capture_prices(symbols: tuple[str, ...], *, resume: bool) -> None:
    for symbol in symbols:
        period_days = PRICE_PERIOD_DAYS[symbol]
        years = 5 if period_days >= 5 * 365 else 3
        path = SAMPLE_ROOT / "prices" / f"{symbol}_prices_{years}y_{CAPTURE_DATE:%Y%m%d}.csv"
        if resume and path.exists():
            print(f"resume skip {path.relative_to(SAMPLE_ROOT)}")
            continue
        bars = fetch_daily_bars(symbol, period_days=period_days)
        rows: list[list[object]] = [["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"]]
        rows.extend([[bar.date, bar.open, bar.high, bar.low, bar.close, bar.adj_close, bar.volume] for bar in bars])
        output = StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        _write_immutable(path, output.getvalue().encode())


def capture_events(*, resume: bool) -> None:
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True) as http:
        jpm_announcement_path = SAMPLE_ROOT / JPM_DIVIDEND_PATH
        if resume and jpm_announcement_path.exists():
            print(f"resume skip {JPM_DIVIDEND_PATH}")
        else:
            response = http.get(JPM_DIVIDEND_URL)
            response.raise_for_status()
            announcement = unescape(response.text)
            required_tokens = ("$1.40 per share", "July 31, 2025", "July 3, 2025")
            if not all(token in announcement for token in required_tokens):
                raise RuntimeError("JPM dividend announcement no longer contains the expected facts")
            normalized = {
                "schema_version": 1,
                "artifact_kind": "extracted_public_statement",
                "source": "jpmorgan_investor_relations",
                "source_url": JPM_DIVIDEND_URL,
                "source_response_sha256": sha256(response.content).hexdigest(),
                "published_on": "2025-05-19",
                "security_type": "common_stock",
                "currency": "USD",
                "amount_per_share": "1.40",
                "record_date": "2025-07-03",
                "pay_date": "2025-07-31",
            }
            body = (json.dumps(normalized, indent=2, sort_keys=True) + "\n").encode()
            _write_immutable(jpm_announcement_path, body)

        for relative_path, url in PUBLIC_ARTIFACTS.items():
            if resume and (SAMPLE_ROOT / relative_path).exists():
                print(f"resume skip {relative_path}")
                continue
            response = http.get(url)
            response.raise_for_status()
            _write_immutable(SAMPLE_ROOT / relative_path, response.content)

        params = {
            "period1": str(int(datetime(2025, 5, 1, tzinfo=UTC).timestamp())),
            "period2": str(int(datetime(2025, 8, 10, tzinfo=UTC).timestamp())),
            "interval": "1d",
            "events": "div,splits",
        }
        jpm_path = SAMPLE_ROOT / "events/JPM_YAHOO_EVENTS_20250501_20250810.json"
        if resume and jpm_path.exists():
            print("resume skip events/JPM_YAHOO_EVENTS_20250501_20250810.json")
        else:
            response = http.get(CHART_URL.format(symbol="JPM"), params=params)
            response.raise_for_status()
            normalized = json.dumps(response.json(), sort_keys=True, separators=(",", ":")).encode()
            _write_immutable(jpm_path, normalized)

        split_params = {
            "period1": str(int(datetime(2024, 5, 1, tzinfo=UTC).timestamp())),
            "period2": str(int(datetime(2024, 7, 1, tzinfo=UTC).timestamp())),
            "interval": "1d",
            "events": "div,splits",
        }
        nvda_path = SAMPLE_ROOT / "events/NVDA_YAHOO_EVENTS_20240501_20240701.json"
        if resume and nvda_path.exists():
            print("resume skip events/NVDA_YAHOO_EVENTS_20240501_20240701.json")
        else:
            response = http.get(CHART_URL.format(symbol="NVDA"), params=split_params)
            response.raise_for_status()
            normalized = json.dumps(response.json(), sort_keys=True, separators=(",", ":")).encode()
            _write_immutable(nvda_path, normalized)


def _capture_paths() -> list[str]:
    paths = [f"sec/{ticker}_CIK{cik:010d}.json" for ticker, cik in COMPANIES.items()]
    paths.extend(SEC_ARTIFACTS)
    paths.append(JPM_DIVIDEND_PATH)
    paths.extend(PUBLIC_ARTIFACTS)
    paths.extend(
        f"prices/{symbol}_prices_{5 if PRICE_PERIOD_DAYS[symbol] >= 5 * 365 else 3}y_{CAPTURE_DATE:%Y%m%d}.csv"
        for symbol in PRICE_SYMBOLS
    )
    paths.extend(
        (
            "events/JPM_YAHOO_EVENTS_20250501_20250810.json",
            "events/NVDA_YAHOO_EVENTS_20240501_20240701.json",
            "golden/DDOG_supply_chain_edges.json",
        )
    )
    return sorted(paths)


def _artifact_source(relative_path: str) -> str:
    if relative_path.startswith("prices/") or "YAHOO_EVENTS" in relative_path:
        return "yahoo"
    if relative_path.startswith("events/JPM_DIVIDEND"):
        return "jpmorgan_investor_relations"
    if relative_path.startswith("events/DDOG_RATING"):
        return "mboum"
    if relative_path.startswith("golden/"):
        return "human_review"
    return "sec"


def write_capture_manifest() -> None:
    path = SAMPLE_ROOT / f"capture_manifest_{CAPTURE_DATE:%Y%m%d}.json"
    if path.exists():
        print(f"resume skip {path.relative_to(SAMPLE_ROOT)}")
        return
    artifacts = []
    for relative_path in _capture_paths():
        artifact_path = SAMPLE_ROOT / relative_path
        if not artifact_path.is_file():
            raise RuntimeError(f"cannot finalize manifest; missing {relative_path}")
        body = artifact_path.read_bytes()
        artifacts.append(
            {
                "path": relative_path,
                "source": _artifact_source(relative_path),
                "byte_length": len(body),
                "sha256": sha256(body).hexdigest(),
            }
        )
    manifest = {
        "schema_version": 1,
        "capture_id": f"strategy-evidence-{CAPTURE_DATE:%Y%m%d}",
        "completed_at": datetime.now(UTC).isoformat(),
        "artifacts": artifacts,
    }
    _write_immutable(path, (json.dumps(manifest, indent=2) + "\n").encode())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--domains",
        default="sec,prices,events",
        help="comma-separated capture domains: sec, prices, events",
    )
    parser.add_argument("--resume", action="store_true", help="skip already captured event artifacts")
    parser.add_argument(
        "--symbols",
        default=",".join(PRICE_SYMBOLS),
        help="comma-separated price symbols; only used when prices is selected",
    )
    args = parser.parse_args()
    domains = {value.strip() for value in args.domains.split(",") if value.strip()}
    unknown = domains - {"sec", "prices", "events"}
    if unknown:
        raise SystemExit(f"unknown domains: {sorted(unknown)}")
    if "sec" in domains:
        capture_sec(resume=args.resume)
    if "prices" in domains:
        symbols = tuple(value.strip().upper() for value in args.symbols.split(",") if value.strip())
        unknown_symbols = set(symbols) - set(PRICE_SYMBOLS)
        if unknown_symbols:
            raise SystemExit(f"unknown price symbols: {sorted(unknown_symbols)}")
        capture_prices(symbols, resume=args.resume)
    if "events" in domains:
        capture_events(resume=args.resume)
    if domains == {"sec", "prices", "events"}:
        write_capture_manifest()


if __name__ == "__main__":
    main()
