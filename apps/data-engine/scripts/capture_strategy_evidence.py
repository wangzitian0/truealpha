#!/usr/bin/env python3
"""Capture bounded public-source evidence set tracked by issue #14."""

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
from data_engine.datahub.fetch_key import FetchKey
from data_engine.sources import gateway
from data_engine.sources.sec import COMPANY_FACTS_URL, company_facts_key
from data_engine.sources.sec import client as sec_client
from data_engine.sources.yahoo import CHART_URL, USER_AGENT, fetch_daily_bars

SAMPLE_ROOT = Path(__file__).resolve().parents[1] / "samples"
CALLER = "capture_strategy_evidence"
CAPTURE_DATE = date(2026, 7, 12)
PRICE_SYMBOLS = ("DDOG", "DUOL", "NICE", "SHOP", "JPM", "ADM", "NVDA", "META", "PLUG")
PRICE_PERIOD_DAYS = {s: 3 * 366 for s in PRICE_SYMBOLS} | {"META": 5 * 366}
COMPANIES = {"JPM": 19617, "ADM": 7084, "NVDA": 1045810, "META": 1326801, "PLUG": 1093691}
SEC_ARTIFACTS = {
    "filings/JPM_10K_000162828026008131.html": (19617, "000162828026008131/jpm-20251231.htm"),
    "filings/ADM_10K_000000708426000011.html": (7084, "000000708426000011/adm-20251231.htm"),
    "filings/NVDA_8K_SPLIT_000104581024000144.html": (1045810, "000104581024000144/nvda-20240607.htm"),
    "filings/NVDA_8K_GUIDANCE_000104581024000113.html": (1045810, "000104581024000113/q1fy25cfocommentary.htm"),
    "filings/META_8K_SYMBOL_000132680122000070.html": (1326801, "000132680122000070/may312022-exhibit991.htm"),
    "filings/PLUG_10K_000155837021007147.html": (1093691, "000155837021007147/plug-20201231x10k.htm"),
    "filings/PLUG_10KA_000155837022003577.html": (1093691, "000155837022003577/plug-20201231x10ka.htm"),
    "nport/QQQ_NPORT_000106783926000016.xml": (1067839, "000106783926000016/primary_doc.xml"),
    "nport/ARKK_NPORT_000094040026012617.xml": (1579982, "000094040026012617/primary_doc.xml"),
}
JPM_DIVIDEND_PATH = "events/JPM_DIVIDEND_20250519.json"
JPM_DIVIDEND_URL = "https://www.jpmorganchase.com/ir/news/2025/jpmc-declares-common-stock-dividend-5-19"
PUBLIC_ARTIFACTS = {"events/DDOG_RATING_CORROBORATION_20260712.html": "https://mboum.com/quotes/DDOG"}


def _write_immutable(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise RuntimeError(f"refusing to overwrite changed evidence: {path}")
        return
    path.write_bytes(body)


def _page_key(url: str) -> FetchKey:
    return FetchKey("public-web", "page", f"url:{url}", gateway.utc_day())


def _chart_key(symbol: str, params: dict[str, str]) -> FetchKey:
    return FetchKey.for_request(
        "yahoo",
        "chart",
        {"symbol": symbol, **params},
        entity_param="symbol",
        as_of_params=("period2",),
        default_as_of=gateway.utc_day(),
    )


def _capture_yahoo_events(
    http: httpx.Client, symbol: str, start: datetime, end: datetime, rel_path: str, resume: bool
) -> None:
    target = SAMPLE_ROOT / rel_path
    if resume and target.exists():
        return
    params = {
        "period1": str(int(start.timestamp())),
        "period2": str(int(end.timestamp())),
        "interval": "1d",
        "events": "div,splits",
    }
    resp = gateway.http_get(
        http,
        "yahoo",
        "chart",
        CHART_URL.format(symbol=symbol),
        identity=_chart_key(symbol, params),
        caller=CALLER,
        params=params,
    )
    resp.raise_for_status()
    _write_immutable(target, json.dumps(resp.json(), sort_keys=True, separators=(",", ":")).encode())


def capture_sec(*, resume: bool) -> None:
    with sec_client() as http:
        for ticker, cik in COMPANIES.items():
            path = SAMPLE_ROOT / "sec" / f"{ticker}_CIK{cik:010d}.json"
            if resume and path.exists():
                continue
            resp = gateway.http_get(
                http,
                "sec",
                "companyfacts",
                COMPANY_FACTS_URL.format(cik=cik),
                identity=company_facts_key(cik),
                caller=CALLER,
            )
            resp.raise_for_status()
            _write_immutable(path, resp.content)
        for rel_path, (cik, archive_path) in SEC_ARTIFACTS.items():
            target = SAMPLE_ROOT / rel_path
            if resume and target.exists():
                continue
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{archive_path}"
            key = FetchKey("sec", "archive", f"filing:{cik:010d}:{archive_path}", gateway.utc_day())
            resp = gateway.http_get(http, "sec", "archive", url, identity=key, caller=CALLER)
            resp.raise_for_status()
            _write_immutable(target, resp.content)


def capture_prices(symbols: tuple[str, ...], *, resume: bool) -> None:
    for symbol in symbols:
        period_days = PRICE_PERIOD_DAYS[symbol]
        years = 5 if period_days >= 5 * 365 else 3
        path = SAMPLE_ROOT / "prices" / f"{symbol}_prices_{years}y_{CAPTURE_DATE:%Y%m%d}.csv"
        if resume and path.exists():
            continue
        bars = fetch_daily_bars(symbol, period_days=period_days)
        output = StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"])
        writer.writerows([[b.date, b.open, b.high, b.low, b.close, b.adj_close, b.volume] for b in bars])
        _write_immutable(path, output.getvalue().encode())


def capture_events(*, resume: bool) -> None:
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True) as http:
        jpm_div = SAMPLE_ROOT / JPM_DIVIDEND_PATH
        if not (resume and jpm_div.exists()):
            resp = gateway.http_get(
                http, "public-web", "page", JPM_DIVIDEND_URL, identity=_page_key(JPM_DIVIDEND_URL), caller=CALLER
            )
            resp.raise_for_status()
            text = unescape(resp.text)
            if not all(t in text for t in ("$1.40 per share", "July 31, 2025", "July 3, 2025")):
                raise RuntimeError("JPM dividend announcement no longer contains the expected facts")
            normalized = {
                "schema_version": 1,
                "artifact_kind": "extracted_public_statement",
                "source": "jpmorgan_investor_relations",
                "source_url": JPM_DIVIDEND_URL,
                "source_response_sha256": sha256(resp.content).hexdigest(),
                "published_on": "2025-05-19",
                "security_type": "common_stock",
                "currency": "USD",
                "amount_per_share": "1.40",
                "record_date": "2025-07-03",
                "pay_date": "2025-07-31",
            }
            _write_immutable(jpm_div, (json.dumps(normalized, indent=2, sort_keys=True) + "\n").encode())

        for rel_path, url in PUBLIC_ARTIFACTS.items():
            target = SAMPLE_ROOT / rel_path
            if resume and target.exists():
                continue
            resp = gateway.http_get(http, "public-web", "page", url, identity=_page_key(url), caller=CALLER)
            resp.raise_for_status()
            _write_immutable(target, resp.content)

        _capture_yahoo_events(
            http,
            "JPM",
            datetime(2025, 5, 1, tzinfo=UTC),
            datetime(2025, 8, 10, tzinfo=UTC),
            "events/JPM_YAHOO_EVENTS_20250501_20250810.json",
            resume,
        )
        _capture_yahoo_events(
            http,
            "NVDA",
            datetime(2024, 5, 1, tzinfo=UTC),
            datetime(2024, 7, 1, tzinfo=UTC),
            "events/NVDA_YAHOO_EVENTS_20240501_20240701.json",
            resume,
        )


def _capture_paths() -> list[str]:
    return sorted(
        [
            *(f"sec/{t}_CIK{c:010d}.json" for t, c in COMPANIES.items()),
            *SEC_ARTIFACTS,
            JPM_DIVIDEND_PATH,
            *PUBLIC_ARTIFACTS,
            *(
                f"prices/{s}_prices_{5 if PRICE_PERIOD_DAYS[s] >= 5 * 365 else 3}y_{CAPTURE_DATE:%Y%m%d}.csv"
                for s in PRICE_SYMBOLS
            ),
            "events/JPM_YAHOO_EVENTS_20250501_20250810.json",
            "events/NVDA_YAHOO_EVENTS_20240501_20240701.json",
            "golden/DDOG_supply_chain_edges.json",
        ]
    )


def _artifact_source(rel: str) -> str:
    if rel.startswith("prices/") or "YAHOO_EVENTS" in rel:
        return "yahoo"
    if rel.startswith("events/JPM_DIVIDEND"):
        return "jpmorgan_investor_relations"
    if rel.startswith("events/DDOG_RATING"):
        return "mboum"
    return "human_review" if rel.startswith("golden/") else "sec"


def write_capture_manifest() -> None:
    path = SAMPLE_ROOT / f"capture_manifest_{CAPTURE_DATE:%Y%m%d}.json"
    if path.exists():
        return
    artifacts = []
    for rel in _capture_paths():
        artifact_path = SAMPLE_ROOT / rel
        if not artifact_path.is_file():
            raise RuntimeError(f"cannot finalize manifest; missing {rel}")
        body = artifact_path.read_bytes()
        artifacts.append(
            {"path": rel, "source": _artifact_source(rel), "byte_length": len(body), "sha256": sha256(body).hexdigest()}
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
    parser.add_argument("--domains", default="sec,prices,events")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--symbols", default=",".join(PRICE_SYMBOLS))
    args = parser.parse_args()
    domains = {v.strip() for v in args.domains.split(",") if v.strip()}
    if unknown := domains - {"sec", "prices", "events"}:
        raise SystemExit(f"unknown domains: {sorted(unknown)}")
    if "sec" in domains:
        capture_sec(resume=args.resume)
    if "prices" in domains:
        symbols = tuple(v.strip().upper() for v in args.symbols.split(",") if v.strip())
        if unknown_sym := set(symbols) - set(PRICE_SYMBOLS):
            raise SystemExit(f"unknown price symbols: {sorted(unknown_sym)}")
        capture_prices(symbols, resume=args.resume)
    if "events" in domains:
        capture_events(resume=args.resume)
    if domains == {"sec", "prices", "events"}:
        write_capture_manifest()


if __name__ == "__main__":
    with gateway.script_run("capture_strategy_evidence"):
        main()
