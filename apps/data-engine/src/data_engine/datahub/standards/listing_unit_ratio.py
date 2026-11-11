"""Depositary share-to-listing unit ratio reader from annual filing iXBRL cover titles (#1519)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from data_engine.datahub.standards.filing_extraction import FilingDocument

log = logging.getLogger(__name__)

TITLE_CONCEPT = "dei:Security12bTitle"
SYMBOL_CONCEPT = "dei:TradingSymbol"

PATTERN_DEPOSITARY = "depositary_each_representing"
PATTERN_PLAIN_SHARE = "plain_share_title"
PATTERN_UNPARSED = "unparsed"

OUTCOME_PARSED = "parsed"
OUTCOME_ORDINARY = "ordinary"
OUTCOME_UNPARSED = "unparsed"
OUTCOME_NOT_MEASURED = "not_measured"
OUTCOMES = (OUTCOME_PARSED, OUTCOME_ORDINARY, OUTCOME_UNPARSED, OUTCOME_NOT_MEASURED)
_OUTCOME_BY_PATTERN = {
    PATTERN_DEPOSITARY: OUTCOME_PARSED,
    PATTERN_PLAIN_SHARE: OUTCOME_ORDINARY,
    PATTERN_UNPARSED: OUTCOME_UNPARSED,
}

REASON_NO_TICKER = "no_ticker"
REASON_NO_ANNUAL_FILING = "no_annual_filing"
REASON_TICKER_NOT_ON_COVER = "ticker_not_on_cover"
REASON_UNFAMILIAR_TITLE = "unfamiliar_title"
REASON_DEPOSITARY_WITHOUT_QUANTITY = "depositary_title_without_quantity"
REASON_TITLES_DISAGREE = "titles_disagree"
REASON_DEBT_SECURITY_ON_EQUITY_TICKER = "debt_security_on_equity_ticker"
REASON_READ_SKIPPED = "read_skipped_after_outage"
REASON_READ_FAILED = "read_failed"

PAYLOAD_KEYS = (
    "listing_units_per_share",
    "ratio_pattern",
    "ratio_title",
    "ratio_reason",
    "ratio_accession",
    "ratio_form",
    "ratio_filed",
)

_TITLE_LIMIT = 400
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "twenty five": 25,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "one hundred": 100,
}
_QUANTITY = (
    r"(?P<quantity>\d{1,4}|"
    + "|".join(w.replace(" ", "[- ]") for w in sorted(_NUMBER_WORDS, key=len, reverse=True))
    + ")"
)
_SHARE = (
    r"(?:(?:class|series) [a-z0-9]+ )?"
    r"(?:(?:ordinary|common|capital|subordinate voting|non[- ]voting|voting) )*"
    r"(?:shares?|stock)"
)
_TAIL = (
    r"(?=\s*$|\s*(?:[,;()]|[-–—])\s*"
    r"|\s+(?:of )?(?:par|nominal|no par|without par)\b|\s+[$€£¥]\s*[\d.])"
)
_DEPOSITARY_WORD = re.compile(r"\b(?:depositary|adss?|adrs?)\b")
_DEPOSITARY_EACH = re.compile(
    r"\b(?:each (?:(?:ads|adr|american depositary share|depositary share) )?"
    r"|(?:one|1) (?:ads|adr|american depositary share|depositary share) )"
    r"(?:representing|represents|represent) "
    r"(?:the right to receive )?" + _QUANTITY + r" " + _SHARE + _TAIL
)
_PLAIN_SHARE = re.compile(r"^" + _SHARE + _TAIL)
_DEBT_NOTE = re.compile(
    r"(?:\b\d+(?:\.\d+)?%\s*(?:(?:senior|subordinated|floating[- ]rate|fixed[- ]rate)\s+)*(?:notes?|debentures?|bonds?)\b"
    r"|\b(?:(?:senior|subordinated|floating[- ]rate|fixed[- ]rate)\s+)*(?:notes?|debentures?|bonds?)\b.*?\bdue\s+\d{4}\b)",
    re.IGNORECASE,
)


def is_debt_security_title(title: str) -> bool:
    return _DEBT_NOTE.search(" ".join(title.split())) is not None


@dataclass(frozen=True)
class TitleReading:
    listing_units_per_share: Decimal | None
    pattern: str
    reason: str | None


def read_title(title: str) -> TitleReading:
    text = " ".join(title.split()).lower()
    if _DEPOSITARY_WORD.search(text):
        match = _DEPOSITARY_EACH.search(text)
        if match is None:
            reason = REASON_DEPOSITARY_WITHOUT_QUANTITY if "representing" not in text else REASON_UNFAMILIAR_TITLE
            return TitleReading(None, PATTERN_UNPARSED, reason)
        quantity = match.group("quantity")
        units = Decimal(quantity) if quantity.isdigit() else Decimal(_NUMBER_WORDS[quantity.replace("-", " ")])
        if units <= 0:
            return TitleReading(None, PATTERN_UNPARSED, REASON_UNFAMILIAR_TITLE)
        return TitleReading(units, PATTERN_DEPOSITARY, None)
    if _PLAIN_SHARE.search(text):
        return TitleReading(Decimal(1), PATTERN_PLAIN_SHARE, None)
    return TitleReading(None, PATTERN_UNPARSED, REASON_UNFAMILIAR_TITLE)


@dataclass(frozen=True)
class ListingUnitRatio:
    listing_units_per_share: Decimal | None
    pattern: str
    title: str | None = None
    reason: str | None = None
    accession: str | None = None
    form: str | None = None
    filed: date | None = None

    def __post_init__(self) -> None:
        parsed = self.listing_units_per_share is not None
        if parsed == (self.pattern == PATTERN_UNPARSED) or parsed == (self.reason is not None):
            raise ValueError(
                f"inconsistent ratio outcome: value={self.listing_units_per_share} "
                f"pattern={self.pattern} reason={self.reason}"
            )

    @property
    def outcome(self) -> str:
        return _OUTCOME_BY_PATTERN[self.pattern]

    def payload_fields(self) -> dict[str, Any]:
        return {
            "listing_units_per_share": None
            if self.listing_units_per_share is None
            else str(self.listing_units_per_share),
            "ratio_pattern": self.pattern,
            "ratio_title": self.title,
            "ratio_reason": self.reason,
            "ratio_accession": self.accession,
            "ratio_form": self.form,
            "ratio_filed": None if self.filed is None else self.filed.isoformat(),
        }


def unparsed(reason: str, *, title: str | None = None, **filing: Any) -> ListingUnitRatio:
    return ListingUnitRatio(None, PATTERN_UNPARSED, title=title, reason=reason, **filing)


def outcome_of_payload(payload: Mapping[str, Any]) -> str:
    if "ratio_pattern" not in payload:
        return OUTCOME_NOT_MEASURED
    return _OUTCOME_BY_PATTERN.get(str(payload["ratio_pattern"]), OUTCOME_UNPARSED)


@dataclass(frozen=True)
class CoverSecurity:
    symbol: str
    title: str


def cover_securities(body: bytes) -> tuple[CoverSecurity, ...]:
    from data_engine.datahub.standards.inline_xbrl import InlineXbrl

    titles: dict[str, list[str]] = {}
    symbols: dict[str, list[str]] = {}
    for fact in InlineXbrl(body).text_facts((TITLE_CONCEPT, SYMBOL_CONCEPT), limit=_TITLE_LIMIT):
        target = titles if fact.concept == TITLE_CONCEPT else symbols
        target.setdefault(fact.context_id, []).append(fact.text)
    paired: list[CoverSecurity] = []
    for context, ctx_titles in titles.items():
        ctx_symbols = symbols.get(context, [])
        if len(set(ctx_symbols)) != 1:
            continue
        non_debt = [title for title in ctx_titles if not is_debt_security_title(title)]
        candidates = non_debt if non_debt else ctx_titles
        if len(set(candidates)) == 1:
            paired.append(CoverSecurity(symbol=ctx_symbols[0], title=candidates[0]))
    return tuple(paired)


def _symbol_key(symbol: str) -> str:
    return symbol.strip().upper().replace("-", ".")


def ratio_for_ticker(
    securities: Sequence[CoverSecurity], ticker: str, *, accession: str, form: str, filed: date
) -> ListingUnitRatio:
    filing: dict[str, Any] = {"accession": accession, "form": form, "filed": filed}
    wanted = _symbol_key(ticker)
    matches = [line for line in securities if _symbol_key(line.symbol) == wanted]
    if not matches:
        return unparsed(REASON_TICKER_NOT_ON_COVER, **filing)
    lines = [line for line in matches if not is_debt_security_title(line.title)]
    if not lines:
        return unparsed(REASON_DEBT_SECURITY_ON_EQUITY_TICKER, title=matches[0].title, **filing)
    readings = [(line, read_title(line.title)) for line in lines]
    for line, reading in readings:
        if reading.listing_units_per_share is None:
            return unparsed(reading.reason or REASON_UNFAMILIAR_TITLE, title=line.title, **filing)
    first_line, first = readings[0]
    if any(reading.listing_units_per_share != first.listing_units_per_share for _, reading in readings):
        return unparsed(REASON_TITLES_DISAGREE, title=" | ".join(line.title for line, _ in readings), **filing)
    return ListingUnitRatio(first.listing_units_per_share, first.pattern, title=first_line.title, **filing)


@dataclass(frozen=True)
class _Cover:
    accession: str
    form: str
    filed: date
    securities: tuple[CoverSecurity, ...]


FilingFinder = Callable[[int, datetime], "FilingDocument | None"]
COVER_OUTAGE_AFTER = 3


def latest_annual_filing_over_http(cik: int, cutoff: datetime) -> FilingDocument | None:
    from data_engine.datahub.standards.filing_extraction import latest_annual_filing
    from data_engine.sources import sec

    with sec.client() as http:
        return latest_annual_filing(cik, http=http, cutoff=cutoff)


class CoverRatioReader:
    def __init__(self, find_filing: FilingFinder | None = None) -> None:
        self._find_filing = find_filing or latest_annual_filing_over_http
        self._covers: dict[tuple[int, date], _Cover | ListingUnitRatio] = {}
        self._failed_in_a_row = 0

    def __call__(self, cik: int, cutoff: date, ticker: str) -> ListingUnitRatio:
        if not ticker.strip():
            return unparsed(REASON_NO_TICKER)
        key = (cik, cutoff)
        if key not in self._covers:
            self._covers[key] = self._read_cover(cik, cutoff)
        cover = self._covers[key]
        if isinstance(cover, ListingUnitRatio):
            return cover
        return ratio_for_ticker(cover.securities, ticker, accession=cover.accession, form=cover.form, filed=cover.filed)

    def _read_cover(self, cik: int, cutoff: date) -> _Cover | ListingUnitRatio:
        from data_engine.datahub.standards.filing_extraction import format_accession

        if self._failed_in_a_row >= COVER_OUTAGE_AFTER:
            return unparsed(REASON_READ_SKIPPED)
        cutoff_at = datetime.combine(cutoff, datetime.max.time(), tzinfo=UTC)
        try:
            document = self._find_filing(cik, cutoff_at)
            self._failed_in_a_row = 0
            if document is None:
                return unparsed(REASON_NO_ANNUAL_FILING)
            return _Cover(
                accession=format_accession(document.accession),
                form=document.form,
                filed=document.filing_date,
                securities=cover_securities(document.body),
            )
        except Exception as error:  # noqa: BLE001
            self._failed_in_a_row = self._failed_in_a_row + 1 if _may_repeat(error) else 0
            log.warning("listing ratio read failed for CIK %s: %s: %s", cik, type(error).__name__, error)
            return unparsed(f"{REASON_READ_FAILED}:{type(error).__name__}")


def _may_repeat(error: Exception) -> bool:
    import httpx

    from data_engine.sources.gateway import CapacityExceeded

    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code != 404
    return isinstance(error, httpx.HTTPError | OSError | CapacityExceeded)
