"""Filing extraction: one field from the issuer's latest annual filing (#70 slice 1, #735).

Recall is a pattern, precision is a judgement. The pattern enumerates every stated
headcount in the filing with its sentence and as-of date; it never missed the right
answer in any multi-candidate case measured (#70). Choosing between candidates is the
model's job when there is a choice — and there is none when the filing states exactly
one company-wide figure. That case is `rule:single-candidate:v1`, a deterministic
extractor with its own identity and its own confidence, so a value landed by it is
distinguishable forever from one a model selected (init.md §9). A filing with several
distinct company-wide statements is `needs_model_selection` until a provider is seated
through the gateway; it is never guessed.

Every vendor request goes through the source gateway (rule 6). The filing's bytes land in
object storage with a `raw.fetches` pointer before any value is read from them, so the
evidence outlives the extractor. `knowable_at` is the filing date — the day the figure
became public — never the fetch clock (rule 12 corollary).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Literal

from truealpha_contracts import DataSource, RawObjectStore
from truealpha_contracts.standards import MetricStandard, confidence_for

from data_engine.datahub.production_topt.headcount import record_headcount
from data_engine.raw_store import insert_fetch
from data_engine.sources.gateway import CapacityExceeded, SourceGateway

ANNUAL_FORMS = ("10-K", "20-F")
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
EXTRACTION_SOURCE = "10k-extraction"
RULE_SINGLE_CANDIDATE = "rule:single-candidate:v1"

# A figure below this is an officer count or a sentence fragment; above it, nothing any
# issuer employs (Walmart's 2.1M is the ceiling in the universe).
_PLAUSIBLE = (10, 5_000_000)

# "we had approximately 33,000 employees", "employed 341,000 employees worldwide",
# "approximately 166,000 full-time equivalent employees" (AAPL), "employed approximately
# 36,500 regular full-time employees" (AMAT), "had approximately 28,000 staff members"
# (AMGN) — the last three were misses on the first real QQQ probe (2026-09-04), so the
# workforce qualifier may stack up to three words. Deliberately loose: a missed candidate
# is unrecoverable downstream, whereas a spurious one is discarded by selection.
_HEADCOUNT = re.compile(
    r"(?:had|employed|have|of|total of)\s+"
    r"(?:approximately\s+|about\s+|over\s+|more than\s+|nearly\s+)?"
    r"([\d][\d,]{2,12})\s+"
    r"(?:(?:full[- ]time|part[- ]time|regular|global|salaried|equivalent|permanent)\s+){0,3}"
    r"(?:employees|persons|people|associates|team members|staff members|colleagues)",
    re.I,
)
_AS_OF = re.compile(r"[Aa]s of ([A-Z][a-z]+ \d{1,2},? \d{4})")
# Inline XBRL leaks concept names into the text ("us-gaap:EmployeeSeveranceMember"); those
# are tag soup, not prose.
_TAG_SOUP = re.compile(r"us-gaap:|Member\b|xbrli:")
# A count qualified as a part of the workforce is not the total. The qualifier is read
# from the count's own CLAUSE (up to the next comma, semicolon or "including"), never
# from the whole sentence: "50,000 people, including 27,000 outside the United States"
# qualifies the 27,000, not the 50,000. The list is deliberately short and literal — a
# false "partial" would silently drop the only candidate — so only phrases that
# unambiguously name a subset are here.
_CLAUSE_END = re.compile(r"[,;.]|\s(?:including|of which|of whom)\s", re.I)
_PARTIAL = re.compile(
    r"\b(part[- ]time|temporary|seasonal|contractors?|contingent|"
    r"in (?:our|the|its) [a-z ]{0,40}(?:segment|division|department|business|function|subsidiar)|"
    r"located in|based in|outside (?:of )?the united states)\b",
    re.I,
)

ExtractionStatus = Literal[
    "resolved",
    "already_recorded",
    "needs_model_selection",
    "no_candidate",
    "no_annual_filing",
    "deferred_capacity",
    "error",
]


@dataclass(frozen=True)
class FilingCandidate:
    value: int
    as_of: str | None
    sentence: str
    partial: bool


@dataclass(frozen=True)
class FilingDocument:
    cik: int
    accession: str
    form: str
    filing_date: date
    primary_document: str
    url: str
    body: bytes


@dataclass(frozen=True)
class ExtractionOutcome:
    cik: int
    status: ExtractionStatus
    extractor: str = RULE_SINGLE_CANDIDATE
    value: int | None = None
    as_of: date | None = None
    sentence: str | None = None
    candidates: tuple[FilingCandidate, ...] = ()
    accession: str | None = None
    form: str | None = None
    filing_date: date | None = None
    raw_fetch_id: int | None = None
    fact_id: int | None = None
    detail: str = ""


def filing_plain_text(body: bytes) -> str:
    text = body.decode("utf-8", "ignore")
    # The ix:header block is a machine-readable duplicate of the whole filing; leaving it
    # in both doubles the text and pollutes it with concept names.
    text = re.sub(r"(?is)<ix:header.*?</ix:header>", " ", text)
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text)


_SENTENCE_END = re.compile(r"(?<=[.;])\s+(?=[A-Z])")


def _sentence(text: str, start: int, end: int) -> str:
    """The sentence containing [start, end): bounded by sentence terminators inside a
    window, so a qualifier in the PREVIOUS sentence never marks this one partial."""
    lo, hi = max(0, start - 220), min(len(text), end + 120)
    head = text[lo:start]
    tail = text[end:hi]
    head_parts = _SENTENCE_END.split(head)
    tail_parts = _SENTENCE_END.split(tail)
    return (head_parts[-1] + text[start:end] + tail_parts[0]).strip()


def candidates(text: str) -> list[FilingCandidate]:
    """Every stated headcount with its sentence, as-of date and a partial/total mark."""
    found: dict[tuple[int, str | None, str], FilingCandidate] = {}
    for match in _HEADCOUNT.finditer(text):
        sentence = _sentence(text, match.start(), match.end())
        if _TAG_SOUP.search(sentence):
            continue
        value = int(match.group(1).replace(",", ""))
        if not _PLAUSIBLE[0] <= value <= _PLAUSIBLE[1]:
            continue
        # The as-of date usually sits in the sentence; a cover-page figure may state it
        # one sentence earlier, so fall back to the surrounding window.
        as_of = _AS_OF.search(sentence) or _AS_OF.search(text[max(0, match.start() - 220) : match.end() + 80])
        clause_tail = text[match.start() : match.end() + 120]
        clause_end = _CLAUSE_END.search(clause_tail, match.end() - match.start())
        clause = clause_tail[: clause_end.start()] if clause_end else clause_tail
        key = (value, as_of.group(1) if as_of else None, sentence)
        found.setdefault(key, FilingCandidate(value, key[1], sentence, bool(_PARTIAL.search(clause))))
    return list(found.values())


def select_total(found: list[FilingCandidate]) -> tuple[ExtractionStatus, FilingCandidate | None]:
    """The deterministic half of selection: one company-wide statement, or defer."""
    totals = [candidate for candidate in found if not candidate.partial]
    distinct = {candidate.value for candidate in totals}
    if not totals:
        # Nothing, or only subset counts (a segment, a region, contractors): the filing
        # states no company-wide total, so there is nothing for a model to choose either.
        return "no_candidate", None
    if len(distinct) == 1:
        return "resolved", totals[0]
    return "needs_model_selection", None


def parse_as_of(text: str | None) -> date | None:
    if text is None:
        return None
    for pattern in ("%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def latest_annual_filing(cik: int, *, http: Any, gateway: SourceGateway, cutoff: datetime) -> FilingDocument | None:
    """The newest 10-K/20-F filed at or before the cutoff, with its primary document."""
    submissions = gateway.call("sec", "submissions", lambda: _get_json(http, SUBMISSIONS_URL.format(cik=cik)))
    recent = submissions["filings"]["recent"]
    cutoff_date = cutoff.astimezone(UTC).date()
    for index, form in enumerate(recent["form"]):
        if form not in ANNUAL_FORMS:
            continue
        filing_date = date.fromisoformat(recent["filingDate"][index])
        if filing_date > cutoff_date:
            continue
        accession = recent["accessionNumber"][index].replace("-", "")
        document = recent["primaryDocument"][index]
        url = ARCHIVE_URL.format(cik=cik, accession=accession, document=document)
        body = gateway.call("sec", "archive", lambda: _get_bytes(http, url))
        return FilingDocument(cik, accession, form, filing_date, document, url, body)
    return None


def _get_json(http: Any, url: str) -> dict[str, Any]:
    response = http.get(url)
    response.raise_for_status()
    return dict(response.json())


def _get_bytes(http: Any, url: str) -> bytes:
    response = http.get(url)
    response.raise_for_status()
    return bytes(response.content)


def extract_headcount(
    cik: int,
    *,
    connection: Any,
    http: Any,
    gateway: SourceGateway,
    standard: MetricStandard,
    cutoff: datetime,
    write: bool,
    store: RawObjectStore | None = None,
    now: datetime | None = None,
) -> ExtractionOutcome:
    """Enumerate, select by rule, and — in write mode — land the cited fact."""
    try:
        document = latest_annual_filing(cik, http=http, gateway=gateway, cutoff=cutoff)
    except CapacityExceeded as error:
        return ExtractionOutcome(cik, "deferred_capacity", detail=str(error))
    except Exception as error:  # noqa: BLE001 - a backfill reports the failure per cell and continues
        return ExtractionOutcome(cik, "error", detail=f"{type(error).__name__}: {error}")
    if document is None:
        return ExtractionOutcome(cik, "no_annual_filing", detail="no 10-K/20-F on file at the cutoff")

    found = candidates(filing_plain_text(document.body))
    status, chosen = select_total(found)
    outcome = ExtractionOutcome(
        cik,
        status,
        value=chosen.value if chosen else None,
        as_of=parse_as_of(chosen.as_of) if chosen else None,
        sentence=chosen.sentence if chosen else None,
        candidates=tuple(found),
        accession=document.accession,
        form=document.form,
        filing_date=document.filing_date,
        detail=f"{len(found)} candidate(s), {len({c.value for c in found if not c.partial})} distinct total(s)",
    )
    if status != "resolved" or not write or chosen is None:
        return outcome
    if _already_recorded(connection, cik, document.accession):
        return ExtractionOutcome(cik, "already_recorded", accession=document.accession, value=chosen.value)

    landed_at = now or datetime.now(UTC)
    raw_id = insert_fetch(
        connection,
        source=DataSource.SEC,
        source_record_id=f"filing-document:CIK{cik:010d}:{document.accession}:{document.primary_document}",
        body=document.body,
        content_type="text/html",
        fetched_at=landed_at,
        source_published_at=datetime.combine(document.filing_date, time.min, tzinfo=UTC),
        metadata={"form": document.form, "accession": document.accession, "cik": cik, "url": document.url},
        store=store,
        recorded_at=landed_at,
    )
    evidence_ref = (
        f"accession={document.accession} form={document.form} filed={document.filing_date.isoformat()} "
        f"raw=raw.fetches:{raw_id} extractor={RULE_SINGLE_CANDIDATE} span={chosen.sentence[:400]!r}"
    )
    fact_id = record_headcount(
        connection,
        cik=cik,
        headcount=Decimal(chosen.value),
        knowable_at=datetime.combine(document.filing_date, time.min, tzinfo=UTC),
        period_end=parse_as_of(chosen.as_of),
        source=EXTRACTION_SOURCE,
        evidence_ref=evidence_ref,
        confidence=confidence_for(standard.confidence_policy_id, RULE_SINGLE_CANDIDATE),
    )
    return ExtractionOutcome(
        cik,
        "resolved",
        value=chosen.value,
        as_of=parse_as_of(chosen.as_of),
        sentence=chosen.sentence,
        candidates=tuple(found),
        accession=document.accession,
        form=document.form,
        filing_date=document.filing_date,
        raw_fetch_id=raw_id,
        fact_id=fact_id,
        detail=outcome.detail,
    )


def _already_recorded(connection: Any, cik: int, accession: str) -> bool:
    row = connection.execute(
        "select 1 from staging.issuer_headcount_facts where cik = %s and source = %s and evidence_ref like %s limit 1",
        (cik, EXTRACTION_SOURCE, f"accession={accession} %"),
    ).fetchone()
    return row is not None
