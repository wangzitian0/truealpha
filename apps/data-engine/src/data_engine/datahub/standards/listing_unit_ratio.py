"""Cover-title listing unit ratio reader (#1519)."""

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
REGISTRANT_CONCEPT = "dei:EntityRegistrantName"

PATTERN_DEPOSITARY = "depositary_each_representing"
PATTERN_PLAIN_SHARE = "plain_share_title"
#: A 10-K filer whose cover names no depositary security: its listed unit is one share, whatever the
#: cover pairs with the ticker (#1606).
PATTERN_DOMESTIC_FILER = "domestic_filer_without_depositary"
PATTERN_UNPARSED = "unparsed"

OUTCOME_PARSED = "parsed"
OUTCOME_ORDINARY = "ordinary"
OUTCOME_UNPARSED = "unparsed"
OUTCOME_NOT_MEASURED = "not_measured"
OUTCOMES = (OUTCOME_PARSED, OUTCOME_ORDINARY, OUTCOME_UNPARSED, OUTCOME_NOT_MEASURED)
_OUTCOME_BY_PATTERN = {
    PATTERN_DEPOSITARY: OUTCOME_PARSED,
    PATTERN_PLAIN_SHARE: OUTCOME_ORDINARY,
    PATTERN_DOMESTIC_FILER: OUTCOME_ORDINARY,
    PATTERN_UNPARSED: OUTCOME_UNPARSED,
}
#: The annual form of a domestic filer. `latest_annual_filing` reads 10-K and 20-F only.
DOMESTIC_ANNUAL_FORMS = frozenset({"10-K", "10-K/A"})

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
    word: index
    for index, word in enumerate(
        "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(),
        1,
    )
} | {"twenty five": 25, "thirty": 30, "forty": 40, "fifty": 50, "one hundred": 100}
_QUANTITY = (
    r"(?P<quantity>\d{1,4}|"
    + "|".join(w.replace(" ", "[- ]") for w in sorted(_NUMBER_WORDS, key=len, reverse=True))
    + ")"
)
_SHARE = (
    r"(?:(?:class|series) [a-z0-9]+ )?"
    r"(?:(?:ordinary|common|capital|registered|subordinate voting|non[- ]voting|voting) )*"
    r"(?:shares?|stock|common(?=,\s*[$€£¥]\s*(?:\d[\d.,]*|\.\d+)\s+par\s+value\s*$))"
    r"(?: of beneficial interest)?"
    r"(?:,? (?:non[- ])?voting)?"
)
_PAR = r"(?:par|nominal|no par|without par)\b"
_TAIL = (
    r"(?=\s*$|\s*(?:[,;()]|[-–—])\s*"
    r"|\.\s*(?:$|[$€£¥]|" + _PAR + r")"
    r"|\s+(?:of )?" + _PAR + r"|\s+[$€£¥]\s*[\d.]"
    r"|\s+of\s+[$€£¥]\s*[\d.]+\s+" + _PAR + r")"
)
_DEPOSITARY_WORD = re.compile(r"\b(?:depositary|adss?|adrs?)\b")
_DEPOSITARY_EACH = re.compile(
    r"\b(?:each (?:(?:ads|adr|american depositary share|depositary share) )?"
    r"|(?:one|1) (?:ads|adr|american depositary share|depositary share) )"
    r"(?:representing|represents|represent) "
    r"(?:the right to receive )?" + _QUANTITY + r" " + _SHARE + _TAIL
)
_PLAIN_SHARE = re.compile(r"^" + _SHARE + _TAIL)
#: The words that may follow the share words of a plain-share title: a par or nominal value clause.
#: Review of #1842: any other word (`; Class B Common Stock`, `, each with one-half of one Warrant`,
#: `- Units`) names a second security, and the title is not read.
_CURRENCY_CODES = "usd eur chf gbp cad dkk sek nok hkd jpy aud nis ils cny rmb inr brl mxn zar sgd nzd twd krw"
_PAR_CLAUSE_WORDS = frozenset(
    ("par value nominal no without per of stated cent cents dollar dollars " + _CURRENCY_CODES).split()
)
_PAR_CLAUSE_SEPARATORS = re.compile(r"[,;:()–—]|(?<![\d\w])-|-(?![\d\w])")
_CURRENCY_PREFIX = r"(?:(?:us|hk|nt|a|c|s|r)?\$|[€£¥]|(?:" + _CURRENCY_CODES.replace(" ", "|") + r"))"
_AMOUNT = re.compile(
    r"^" + _CURRENCY_PREFIX + r"?(?:\d+(?:[.,]\d+)*|\.\d+)(?:-\d+/\d+)?¢?$|^" + _CURRENCY_PREFIX + r"$"
)
_FRACTION = re.compile(r"^\d+/\d+¢?$")
#: A rights plan attaches one right to each share; the listing stays one share.
_ASSOCIATED_RIGHTS = re.compile(
    r"[,;]?\s*\(?\s*(?:and\s+|with\s+)?(?:the\s+)?associated\s+(?:[a-z-]+\s+){0,5}rights\s*\)?\.?$"
)
_FOOTNOTE = re.compile(r"\s*(?:\*+|\(\d\))$")


def _only_a_par_clause(rest: str) -> bool:
    """Each word after the share words belongs to one par or nominal value clause. Position rules
    keep a second security out: `share` only after `per`, `each` and `voting` only last, `one`
    only before `dollar`, a fraction only after an amount (#1842 review)."""
    rest = _ASSOCIATED_RIGHTS.sub("", _FOOTNOTE.sub("", rest))
    words = [word.strip(".") for word in _PAR_CLAUSE_SEPARATORS.sub(" ", rest).split()]
    words = [word for word in words if word]
    for index, word in enumerate(words):
        before = words[index - 1] if index else ""
        after = words[index + 1] if index + 1 < len(words) else ""
        last = index == len(words) - 1
        if word in _PAR_CLAUSE_WORDS or _AMOUNT.match(word):
            continue
        if word == "share" and before == "per":
            continue
        if word in ("each", "voting") and last:
            continue
        if word == "one" and after in ("dollar", "dollars"):
            continue
        if _FRACTION.match(word) and _AMOUNT.match(before) and any(ch.isdigit() for ch in before):
            continue
        return False
    return True


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


def read_title(title: str, registrants: Sequence[str] = ()) -> TitleReading:
    """`registrants` are the filer names of the cover: a title may name its own filer (#1606)."""
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
    bare = without_registrant(text, registrants)
    if (match := _PLAIN_SHARE.match(bare)) is not None and _only_a_par_clause(bare[match.end() :]):
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


def cover_titles(body: bytes) -> tuple[str, ...]:
    """Every security title of the cover, paired with a ticker or not."""
    from data_engine.datahub.standards.inline_xbrl import InlineXbrl

    return tuple(fact.text for fact in InlineXbrl(body).text_facts((TITLE_CONCEPT,), limit=_TITLE_LIMIT))


def cover_registrants(body: bytes) -> tuple[str, ...]:
    """The filer names of the cover. A combined 10-K names several."""
    from data_engine.datahub.standards.inline_xbrl import InlineXbrl

    return tuple(fact.text for fact in InlineXbrl(body).text_facts((REGISTRANT_CONCEPT,), limit=_TITLE_LIMIT))


def names_a_depositary_security(titles: Sequence[str]) -> bool:
    return any(_DEPOSITARY_WORD.search(" ".join(title.split()).lower()) for title in titles)


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


def _name_pattern(name: str) -> re.Pattern[str] | None:
    """A filer name as whole words; commas and periods between and after its words may differ."""
    words = re.findall(r"[^\s,.]+", name)
    if not words:
        return None
    return re.compile(r"(?:\bof\s+)?(?<![\w])" + r"[\s,.]*".join(map(re.escape, words)) + r"(?![\w])\.?", re.IGNORECASE)


def without_registrant(title: str, registrants: Sequence[str]) -> str:
    """The title without one filer name, the longest that it holds, with an `of` before it (#1606):
    `Evergy, Inc. common stock` and `Common Stock of PPL Corporation` keep only the security.
    A second filer name of a combined report stays, so the grammar refuses that title."""
    text = " ".join(title.split())
    for pattern in (_name_pattern(name) for name in sorted(set(registrants), key=len, reverse=True)):
        if pattern is not None and pattern.search(text):
            return " ".join(pattern.sub(" ", text).split()).strip(" ,")
    return text


def ratio_for_ticker(
    securities: Sequence[CoverSecurity],
    ticker: str,
    *,
    accession: str,
    form: str,
    filed: date,
    titles: Sequence[str] = (),
    registrants: Sequence[str] = (),
) -> ListingUnitRatio:
    """`titles` are every security title that the cover tags, paired with a ticker or not.
    `registrants` are the filer names of the cover (`dei:EntityRegistrantName`)."""
    filing: dict[str, Any] = {"accession": accession, "form": form, "filed": filed}
    wanted = _symbol_key(ticker)
    matches = [line for line in securities if _symbol_key(line.symbol) == wanted]
    if not matches:
        # A cover with no tagged title (an older filing, an 8-K) is no evidence; only a read cover is.
        if form in DOMESTIC_ANNUAL_FORMS and titles and not names_a_depositary_security(titles):
            return ListingUnitRatio(Decimal(1), PATTERN_DOMESTIC_FILER, **filing)
        return unparsed(REASON_TICKER_NOT_ON_COVER, **filing)
    lines = [line for line in matches if not is_debt_security_title(line.title)]
    if not lines:
        return unparsed(REASON_DEBT_SECURITY_ON_EQUITY_TICKER, title=matches[0].title, **filing)
    readings = [(line, read_title(line.title, registrants)) for line in lines]
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
    titles: tuple[str, ...] = ()
    registrants: tuple[str, ...] = ()


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
        return ratio_for_ticker(
            cover.securities,
            ticker,
            accession=cover.accession,
            form=cover.form,
            filed=cover.filed,
            titles=cover.titles,
            registrants=cover.registrants,
        )

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
                titles=cover_titles(document.body),
                registrants=cover_registrants(document.body),
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
