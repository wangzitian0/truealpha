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

from dataclasses import dataclass

# Home market first; 'US' last so it only wins for genuinely US-listed instruments.
_EXCH_PRIORITY = ("HK", "CG", "CS", "CH", "US")

# A-share board by code range, for records that only expose the 'CH' composite:
# 6xxxxx (incl. 688 STAR) / 9xxxxx (B) trade in Shanghai; 0xxxxx / 2xxxxx (B) /
# 3xxxxx (ChiNext) in Shenzhen.
_SH_LEADING = ("6", "9")
_SZ_LEADING = ("0", "2", "3")


@dataclass(frozen=True)
class Listing:
    exch_token: str  # leading token of the OpenFIGI exchCode
    ticker: str
    name: str | None
    security_type: str | None


def _exch_token(record: dict) -> str | None:
    exch = record.get("exchCode")
    return exch.split()[0] if exch else None


def pick_listing(records: list[dict]) -> Listing | None:
    """The primary listing among an ISIN's venue records, or None if no market we
    rank appears (e.g. Taiwan-only names)."""
    best = None
    best_rank = len(_EXCH_PRIORITY)
    for record in records:
        if record.get("marketSector") != "Equity":
            continue
        token = _exch_token(record)
        if token not in _EXCH_PRIORITY:
            continue
        rank = _EXCH_PRIORITY.index(token)
        if rank < best_rank and record.get("ticker"):
            best = Listing(
                exch_token=token,
                ticker=record["ticker"],
                name=record.get("name"),
                security_type=record.get("securityType"),
            )
            best_rank = rank
    return best


def moomoo_code(listing: Listing) -> tuple[str, float] | None:
    """(moomoo code, rule confidence) for a listing, or None if moomoo has no such
    market. Confidence dips for 'CH' composite records, where the board is inferred
    from the A-share code range instead of stated by OpenFIGI."""
    token, ticker = listing.exch_token, listing.ticker
    if token == "US":
        return f"US.{ticker.replace('/', '.')}", 0.98
    if token == "HK":
        return f"HK.{ticker.zfill(5)}", 0.98
    if token == "CG":
        return f"SH.{ticker}", 0.98
    if token == "CS":
        return f"SZ.{ticker}", 0.98
    if token == "CH":
        if ticker.startswith(_SH_LEADING):
            return f"SH.{ticker}", 0.9
        if ticker.startswith(_SZ_LEADING):
            return f"SZ.{ticker}", 0.9
    return None


def sec_ticker(listing: Listing) -> str | None:
    """SEC-format ticker for a US listing (SEC uses 'BRK-B' where Bloomberg/OpenFIGI
    use 'BRK/B'); None for non-US listings."""
    if listing.exch_token != "US":
        return None
    return listing.ticker.replace("/", "-").upper()
