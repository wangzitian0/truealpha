"""The depositary ratio of a listing, read from the cover of the issuer's annual filing (#1519).

A price is per listed unit. An American depositary share (ADS) stands for several ordinary shares:
one PDD ADS stands for four Class A ordinary shares. The share count of the filing is in ordinary
shares. No ingested field carries the ratio. The cover of a 10-K or 20-F prints it as free text in
the title of the security (`dei:Security12bTitle`). The same context holds the ticker
(`dei:TradingSymbol`).

This module reads that title with a CLOSED set of patterns:

- A depositary title with a quantity ("each representing four Class A ordinary shares") gives that
  quantity. The pattern is `depositary_each_representing`.
- A plain share title without a depositary word ("Common Stock, par value $0.01") gives 1. The
  pattern is `plain_share_title`.
- Any other title gives no value. The result is `unparsed` and keeps the title text and a reason.

A pattern never guesses. A fraction ("one-half of one ordinary share"), a preferred share, a note
and every unfamiliar wording fall to `unparsed`. The measured result is a dashboard row. No code
reads the value yet (#1519, step 1a).

`listing_units_per_share` is the number of ordinary shares that ONE listed unit stands for. A plain
share gives 1. One PDD ADS gives 4.
"""

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

#: What the dashboard says about one listing. `not_measured` is a payload that has no ratio key: the
#: observation is older than the reader, or no reader ran. It is not an `unparsed` title.
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
#: The reads of this run ended after `COVER_OUTAGE_AFTER` shared failures in a row.
REASON_READ_SKIPPED = "read_skipped_after_outage"
#: Followed by `:` and the class name of the error, for example `read_failed:HTTPStatusError`.
REASON_READ_FAILED = "read_failed"

#: The keys that a payload carries when the reader ran. One list: the adapter writes them, the
#: payload model declares them and the quality report reads them.
PAYLOAD_KEYS = (
    "listing_units_per_share",
    "ratio_pattern",
    "ratio_title",
    "ratio_reason",
    "ratio_accession",
    "ratio_form",
    "ratio_filed",
)

#: The longest title text that the reader keeps. A real cover title is under 200 characters.
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
# The longest word first: "twenty five" and "one hundred" must win over "twenty" and "one". A hyphen
# or a space joins the two words.
_QUANTITY = (
    r"(?P<quantity>\d{1,4}|"
    + "|".join(w.replace(" ", "[- ]") for w in sorted(_NUMBER_WORDS, key=len, reverse=True))
    + ")"
)

# One class of share: an optional class mark, an optional kind, then the noun.
_SHARE = (
    r"(?:(?:class|series) [a-z0-9]+ )?"
    r"(?:(?:ordinary|common|capital|subordinate voting) )?"
    r"(?:shares?|stock)"
)
# What may follow the share noun. A tail that is not in this list (for example "of 5.75% preferred")
# means the title is about another security, so the pattern does not match.
_TAIL = r"(?=\s*$|\s*[,;(]|\s+(?:of )?(?:par|nominal|no par|without par)\b|\s+[$€£¥]\s*[\d.])"

_DEPOSITARY_WORD = re.compile(r"\b(?:depositary|adss?|adrs?)\b")
_DEPOSITARY_EACH = re.compile(
    r"\beach (?:(?:ads|adr|american depositary share|depositary share) )?(?:representing|represents|represent) "
    r"(?:the right to receive )?" + _QUANTITY + r" " + _SHARE + _TAIL
)
_PLAIN_SHARE = re.compile(r"^" + _SHARE + _TAIL)


@dataclass(frozen=True)
class TitleReading:
    """What the closed patterns say about one title."""

    listing_units_per_share: Decimal | None
    pattern: str
    reason: str | None


def read_title(title: str) -> TitleReading:
    """Read one cover title. A title that no pattern matches gives no value and a reason."""
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
    """The outcome of the cover read for one listing, and the filing that it came from.

    A value and a reason exclude each other: a parsed outcome has no reason, and an `unparsed`
    outcome has no value. The title text stays on both, so a reader can see what was parsed.
    """

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
        """The payload keys of the financial-fact observation. All seven are present, null where empty."""
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
    """An outcome with no value. `filing` carries `accession`, `form` and `filed` when the filing is known."""
    return ListingUnitRatio(None, PATTERN_UNPARSED, title=title, reason=reason, **filing)


def outcome_of_payload(payload: Mapping[str, Any]) -> str:
    """The dashboard outcome of one financial-fact payload. A payload with no ratio key is `not_measured`."""
    if "ratio_pattern" not in payload:
        return OUTCOME_NOT_MEASURED
    return _OUTCOME_BY_PATTERN.get(str(payload["ratio_pattern"]), OUTCOME_UNPARSED)


@dataclass(frozen=True)
class CoverSecurity:
    """One line of the cover: the ticker and the title of the security that trades under it."""

    symbol: str
    title: str


def cover_securities(body: bytes) -> tuple[CoverSecurity, ...]:
    """The cover lines of an inline XBRL filing that pair one title with one ticker in one context.

    A title with no ticker in its context (the notes of a bank, "Not for trading" lines) is left out.
    A context with several titles or several tickers cannot be paired and is left out.
    """
    from data_engine.datahub.standards.inline_xbrl import InlineXbrl

    titles: dict[str, list[str]] = {}
    symbols: dict[str, list[str]] = {}
    for fact in InlineXbrl(body).text_facts((TITLE_CONCEPT, SYMBOL_CONCEPT), limit=_TITLE_LIMIT):
        target = titles if fact.concept == TITLE_CONCEPT else symbols
        target.setdefault(fact.context_id, []).append(fact.text)
    return tuple(
        CoverSecurity(symbol=symbols[context][0], title=titles[context][0])
        for context in titles
        if len(set(titles[context])) == 1 and len(set(symbols.get(context, ()))) == 1
    )


def _symbol_key(symbol: str) -> str:
    """The comparison form of a ticker: upper case, with the share-class dot (BRK-B and BRK.B are one)."""
    return symbol.strip().upper().replace("-", ".")


def ratio_for_ticker(
    securities: Sequence[CoverSecurity], ticker: str, *, accession: str, form: str, filed: date
) -> ListingUnitRatio:
    """The ratio of the cover line under `ticker`.

    Two lines under one ticker must agree on the value. If they differ, or one does not parse, the
    result has no value: the cover does not say which line the listing is (#1519, U7).
    """
    filing: dict[str, Any] = {"accession": accession, "form": form, "filed": filed}
    wanted = _symbol_key(ticker)
    lines = [line for line in securities if _symbol_key(line.symbol) == wanted]
    if not lines:
        return unparsed(REASON_TICKER_NOT_ON_COVER, **filing)
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
    """The paired cover lines of one filing, kept in place of its bytes."""

    accession: str
    form: str
    filed: date
    securities: tuple[CoverSecurity, ...]


FilingFinder = Callable[[int, datetime], "FilingDocument | None"]

#: Reads in a row that fail in a way the next issuer may share (a timeout, a reset, a block, a 5xx, a
#: spent budget) end the reads of the run. Each failing read can wait for the 30 s timeout of the SEC
#: client, so a host that is down would cost 30 s for each issuer. The ratio is a measurement: ending it
#: costs a dashboard row, never a served value. The next run reads again.
COVER_OUTAGE_AFTER = 3


def latest_annual_filing_over_http(cik: int, cutoff: datetime) -> FilingDocument | None:
    """The newest 10-K or 20-F filed at or before `cutoff`, through the source gateway.

    The submissions index has one key per CIK per UTC day, and the issuer registry reads it first.
    The primary document has one key per accession, held for ever, and the standards lane shares
    it. A key that landed makes no SEC call (#1411 K3, K5).
    """
    from data_engine.datahub.standards.filing_extraction import latest_annual_filing
    from data_engine.sources import sec

    with sec.client() as http:
        return latest_annual_filing(cik, http=http, cutoff=cutoff)


class CoverRatioReader:
    """Reads the ratio of one listing from the cover of its issuer's latest annual filing.

    One reader serves one run. It keeps the paired cover lines of each (CIK, cutoff), not the filing
    bytes, so two listings of one issuer (GOOG and GOOGL) read one filing and parse it once. A call
    never raises: a read that fails gives an `unparsed` outcome that names the error class. The
    issuer's numbers are captured whatever this read does.
    """

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
        # The end of the cutoff day: a filing filed on the cutoff date is knowable at the cutoff.
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
        except Exception as error:  # noqa: BLE001 - the ratio is a measurement; its failure never aborts a capture
            self._failed_in_a_row = self._failed_in_a_row + 1 if _may_repeat(error) else 0
            log.warning("listing ratio read failed for CIK %s: %s: %s", cik, type(error).__name__, error)
            return unparsed(f"{REASON_READ_FAILED}:{type(error).__name__}")


def _may_repeat(error: Exception) -> bool:
    """Whether the next issuer may meet the same failure: a network error, a block, a 5xx, a spent budget.

    A 404 is a missing document of one issuer. An error of the parse is a fault of one document.
    """
    import httpx

    from data_engine.sources.gateway import CapacityExceeded

    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code != 404
    return isinstance(error, httpx.HTTPError | OSError | CapacityExceeded)
