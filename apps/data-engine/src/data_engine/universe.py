"""Universe-construction rules: pick a holding's primary listing from its
OpenFIGI records and derive market-local codes (moomoo, SEC ticker).

The exchCode values and their quirks were verified live against OpenFIGI on
2026-07-11 (NVDA / Tencent / Alibaba Health / BYD Electronic / Moutai / Midea /
CATL A+H / MediaTek / TME):

- 'US'  composite (real US listings AND ADRs; ticker as Bloomberg-style 'BRK/B')
- 'HK'  Hong Kong (ticker is the bare number, '700' -> moomoo wants 'HK.00700')
- 'CG'  Shanghai, 'CS' Shenzhen, 'CH' China composite (either board),
  'C1'/'C2' Stock Connect duplicates of the same lines
- 'TT'  Taiwan — sometimes suffixed ('TT (Taiwan Stock Exchange)'), so match the
  leading token; no moomoo market exists for it
- HK/CN-listed names carry a US OTC record under the SAME ISIN (Tencent ->
  'TCTZF') — the home market must outrank 'US' or every Chinese name maps to an
  illiquid pink-sheet line.
"""

import re
from dataclasses import dataclass

# Home market first; 'US' last so it only wins for genuinely US-listed
# instruments. For US-registered ISINs this order is REVERSED (see
# pick_listing): HKEX lists thinly-traded secondary lines of US blue chips
# (HK.04338 is Microsoft) under the same US ISIN, and those must not outrank
# the US composite the way Tencent's home line outranks its US OTC record.
_EXCH_PRIORITY = ("HK", "CG", "CS", "CH", "US")
_EXCH_PRIORITY_US_ISIN = ("US", "HK", "CG", "CS", "CH")

# A-share board by code range, for records that only expose the 'CH' composite:
# 6xxxxx (incl. 688 STAR) / 9xxxxx (B) trade in Shanghai; 0xxxxx / 2xxxxx (B) /
# 3xxxxx (ChiNext) in Shenzhen.
_SH_LEADING = ("6", "9")
_SZ_LEADING = ("0", "2", "3")

_MARKET_METADATA = {
    "US": ("USD", "America/New_York", "XNYS"),
    "HK": ("HKD", "Asia/Hong_Kong", "XHKG"),
    "CG": ("CNY", "Asia/Shanghai", "XSHG"),
    "CS": ("CNY", "Asia/Shanghai", "XSHE"),
    # A CH composite is board-ambiguous. Callers must resolve it through the
    # ticker range before using this metadata for an executable listing.
    "CH": ("CNY", "Asia/Shanghai", "XCN"),
}


@dataclass(frozen=True)
class Listing:
    exch_token: str  # leading token of the OpenFIGI exchCode
    ticker: str
    name: str | None
    security_type: str | None
    resolution_method: str = "openfigi_ranked"
    confidence: float = 0.98


def _exch_token(record: dict) -> str | None:
    exch = record.get("exchCode")
    return exch.split()[0] if exch else None


def pick_listing(records: list[dict], isin: str | None = None) -> Listing | None:
    """The primary listing among an ISIN's venue records, or None if no market we
    rank appears (e.g. Taiwan-only names). The ISIN's country prefix decides which
    priority order applies — a US-registered instrument's home market is the US
    composite even when an HKEX secondary line exists."""
    priority = _EXCH_PRIORITY_US_ISIN if isin is not None and isin.startswith("US") else _EXCH_PRIORITY
    best = None
    best_rank = len(priority)
    for record in records:
        if record.get("marketSector") != "Equity":
            continue
        token = _exch_token(record)
        if token not in priority:
            continue
        rank = priority.index(token)
        if rank < best_rank and record.get("ticker"):
            best = Listing(
                exch_token=token,
                ticker=record["ticker"],
                name=record.get("name"),
                security_type=record.get("securityType"),
            )
            best_rank = rank
    return best


_CORPORATE_SUFFIXES = {
    "co",
    "company",
    "corp",
    "corporation",
    "inc",
    "incorporated",
    "limited",
    "ltd",
    "plc",
}


def _normalized_issuer_name(value: str) -> tuple[str, ...]:
    words = re.findall(r"[a-z0-9]+", value.lower())
    while words and words[-1] in _CORPORATE_SUFFIXES:
        words.pop()
    return tuple(words)


def resolve_listing(
    records: list[dict],
    *,
    isin: str | None,
    issuer_name: str | None,
    sec_ticker_map: dict[str, tuple[int, str]],
) -> Listing | None:
    """Resolve a primary listing, with a fail-closed SEC corroboration fallback.

    OpenFIGI occasionally returns only foreign venue rows for a U.S. ISIN. In
    that case a repeated ticker is still useful evidence, but it is accepted as
    the U.S. line only when SEC's official ticker map contains it and SEC's
    issuer name normalizes exactly to the N-PORT issuer name. Ambiguous matches
    remain unresolved.
    """

    ranked = pick_listing(records, isin)
    if ranked is not None or not isin or not isin.startswith("US") or not issuer_name:
        return ranked

    expected_name = _normalized_issuer_name(issuer_name)
    candidates: dict[str, dict] = {}
    for record in records:
        ticker = record.get("ticker")
        if record.get("marketSector") != "Equity" or not ticker:
            continue
        sec_symbol = str(ticker).replace("/", "-").upper()
        sec_row = sec_ticker_map.get(sec_symbol)
        if sec_row is None or _normalized_issuer_name(sec_row[1]) != expected_name:
            continue
        candidates[sec_symbol] = record

    if len(candidates) != 1:
        return None
    sec_symbol, evidence = next(iter(candidates.items()))
    return Listing(
        exch_token="US",
        ticker=sec_symbol.replace("-", "/"),
        name=evidence.get("name") or issuer_name,
        security_type=evidence.get("securityType"),
        resolution_method="openfigi_sec_name_fallback",
        confidence=0.9,
    )


def moomoo_code(listing: Listing) -> tuple[str, float] | None:
    """(moomoo code, rule confidence) for a listing, or None if moomoo has no such
    market. Confidence dips for 'CH' composite records, where the board is inferred
    from the A-share code range instead of stated by OpenFIGI."""
    token, ticker = listing.exch_token, listing.ticker
    if token == "US":
        return f"US.{ticker.replace('/', '.')}", min(0.98, listing.confidence)
    if token == "HK":
        return f"HK.{ticker.zfill(5)}", min(0.98, listing.confidence)
    if token == "CG":
        return f"SH.{ticker}", min(0.98, listing.confidence)
    if token == "CS":
        return f"SZ.{ticker}", min(0.98, listing.confidence)
    if token == "CH":
        if ticker.startswith(_SH_LEADING):
            return f"SH.{ticker}", min(0.9, listing.confidence)
        if ticker.startswith(_SZ_LEADING):
            return f"SZ.{ticker}", min(0.9, listing.confidence)
    return None


def sec_ticker(listing: Listing) -> str | None:
    """SEC-format ticker for a US listing (SEC uses 'BRK-B' where Bloomberg/OpenFIGI
    use 'BRK/B'); None for non-US listings."""
    if listing.exch_token != "US":
        return None
    return listing.ticker.replace("/", "-").upper()


def market_metadata(exch_token: str) -> tuple[str, str, str]:
    """Return currency, IANA timezone, and exchange calendar for a ranked market."""
    try:
        return _MARKET_METADATA[exch_token]
    except KeyError as exc:
        raise ValueError(f"unsupported ranked market: {exch_token}") from exc
