"""Primary equity listing selection and local code formatting."""

from dataclasses import dataclass

# Home market outranks US OTC lines unless ISIN is US-registered.
_EXCH_PRIORITY = ("HK", "CG", "CS", "CH", "US")
_EXCH_PRIORITY_US_ISIN = ("US", "HK", "CG", "CS", "CH")
_SH_LEADING = ("6", "9")
_SZ_LEADING = ("0", "2", "3")
_BOARD_PREFIX = {"CG": "SH", "CS": "SZ"}


@dataclass(frozen=True)
class Listing:
    exch_token: str
    ticker: str
    name: str | None


def _exch_token(record: dict) -> str | None:
    exch = record.get("exchCode")
    return exch.split()[0] if exch else None


def pick_listing(records: list[dict], isin: str | None = None) -> Listing | None:
    """Select primary equity listing from OpenFIGI records."""
    priority = _EXCH_PRIORITY_US_ISIN if isin is not None and isin.startswith("US") else _EXCH_PRIORITY
    best = None
    best_rank = len(priority)
    for record in records:
        if record.get("marketSector") != "Equity":
            continue
        token = _exch_token(record)
        if token in priority and (rank := priority.index(token)) < best_rank and record.get("ticker"):
            best = Listing(exch_token=token, ticker=record["ticker"], name=record.get("name"))
            best_rank = rank
    return best


def moomoo_code(listing: Listing) -> tuple[str, float] | None:
    """Return `(code, confidence)` or None if unsupported."""
    token, ticker = listing.exch_token, listing.ticker
    if token == "US":
        return f"US.{ticker.replace('/', '.')}", 0.98
    if token == "HK":
        return f"HK.{ticker.zfill(5)}", 0.98
    if token in _BOARD_PREFIX:
        return f"{_BOARD_PREFIX[token]}.{ticker}", 0.98
    if token == "CH":
        if ticker.startswith(_SH_LEADING):
            return f"SH.{ticker}", 0.9
        if ticker.startswith(_SZ_LEADING):
            return f"SZ.{ticker}", 0.9
    return None


def sec_ticker(listing: Listing) -> str | None:
    return listing.ticker.replace("/", "-").upper() if listing.exch_token == "US" else None
