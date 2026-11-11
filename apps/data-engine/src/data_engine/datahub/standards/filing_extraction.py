"""SEC filing fetcher, multi-class share cover reader, and headcount adapter (rule:single-candidate:v1)."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from factors.shared import extraction as extraction_primitive
from truealpha_contracts.models import DataSource
from truealpha_contracts.ports import RawObjectStore
from truealpha_contracts.standards import MetricStandard, confidence_for

from data_engine.datahub.fetch_key import FetchKey
from data_engine.datahub.production_topt.headcount import record_headcount
from data_engine.datahub.production_topt.issuer_registry import ANNUAL_STATEMENT_FORMS, STATEMENT_FORMS
from data_engine.datahub.production_topt.source_registrations import AsOfAxis, knowable_instant
from data_engine.raw_store import insert_fetch
from data_engine.sources import llm
from data_engine.sources.gateway import CapacityExceeded, SourceGateway
from data_engine.sources.sec import SUBMISSIONS_URL, submissions_key

ANNUAL_FORMS = ("10-K", "20-F")
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
ACCESSION_AS_OF = "accession"
SUBMISSIONS_OVERFLOW_URL = "https://data.sec.gov/submissions/{name}"
EXTRACTION_SOURCE = "10k-extraction"
RULE_SINGLE_CANDIDATE = extraction_primitive.RULE_SINGLE_CANDIDATE

_PLAUSIBLE = (10, 5_000_000)
_SCALE_MULTIPLIERS = {"thousand": Decimal(1_000), "million": Decimal(1_000_000)}
_HEADCOUNT = re.compile(
    r"(?:had|employed|have|includes?|of|total of|our|headcount(?:\s+increased)?(?:\s+(?:\d+(?:\.\d+)?%\s+)?(?:from\s+[\d,]+\s+)?)?to)\s+"
    r"(?:approximately\s+|approxima\s+tely\s+|about\s+|over\s+|more than\s+|nearly\s+)?"
    r"([\d][\d,]*(?:\.\d+)?)\s+(?:(?:[1-9]|\(\d\))\s+)?(?:(million|thousand)\s+)?"
    r"(?:(?:full(?:[- ]time)?|part[- ]time|temporary|regular|global|total|salaried|equivalent|permanent)(?:,\s*|\s+(?:and|or)\s+|\s+)){0,4}"
    r"(?:employees|employee-partners|persons|people|associates|a\s+ssociates|workers|team members|staff members|colleagues)"
    r"|(?:employee headcount(?:\s+worldwide)?|full[- ]time count for [a-z' ]{1,35}employees(?:\s+worldwide)?)\s+was\s+"
    r"(?:approximately\s+|about\s+)?([\d][\d,]*(?:\.\d+)?)(?:\s+(million|thousand))?"
    r"|(?:global\s+workforce\s+of|total\s+employees)\s+([\d][\d,]*(?:\.\d+)?)(?:\s+(million|thousand))?",
    re.I,
)
_AS_OF = re.compile(r"(?:[Aa]s of|[Aa]t) ([A-Z][a-z]+ \d{1,2},? \d{4})")
_TAG_SOUP = re.compile(r"us-gaap:|Member\b|xbrli:")
_CLAUSE_END = re.compile(
    r"[,;.]|\s(?:including|of which|of whom)\s|\s+and\s+(?:approximately|approxima\s+tely|about|over|more than|nearly|\d)",
    re.I,
)
_PARTIAL = re.compile(
    r"\b((?<!full[- ]time and )(?<!full[- ]time or )(?<!full and )(?<!full-time, )part[- ]time|"
    r"(?<!part[- ]time and )(?<!part[- ]time or )temporary|seasonal|contractors?|contingent|"
    r"(?:research and development|sales and marketing|general and administrative|engineering)\s+(?:staff|employees|headcount|personnel|team)|"
    r"in (?:our|the|its) [a-z ]{0,40}(?:segment|division|department|business|function|subsidiar)|"
    r"located in|based in|outside (?:of )?the united states)\b",
    re.I,
)

ExtractionStatus = Literal[
    "resolved",
    "already_recorded",
    "needs_model_selection",
    "model_declined",
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
    model_replayed: bool | None = None


def filing_plain_text(body: bytes) -> str:
    text = body.decode("utf-8", "ignore")
    text = re.sub(r"(?is)<ix:header.*?</ix:header>", " ", text)
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text)


_SENTENCE_END = re.compile(r"(?<=[.;])\s+(?=[A-Z])")


def _sentence(text: str, start: int, end: int) -> str:
    lo, hi = max(0, start - 220), min(len(text), end + 120)
    head_parts = _SENTENCE_END.split(text[lo:start])
    tail_parts = _SENTENCE_END.split(text[end:hi])
    return (head_parts[-1] + text[start:end] + tail_parts[0]).strip()


def _parse_headcount_value(raw_number: str | None, raw_scale: str | None) -> int | None:
    if not raw_number:
        return None
    cleaned = raw_number.replace(",", "")
    scale = _SCALE_MULTIPLIERS.get(raw_scale.lower()) if raw_scale else None
    if scale is None and ("." in cleaned or len(cleaned) < 3 or len(cleaned) > 13):
        return None
    try:
        scaled = Decimal(cleaned) * (scale if scale is not None else Decimal(1))
    except InvalidOperation:
        return None
    return int(scaled) if scaled == int(scaled) else None


def candidates(text: str) -> list[FilingCandidate]:
    """Enumerate all stated headcount figures in `text` with sentence span, as-of date, and partial mark."""
    found: dict[tuple[int, str | None, str], FilingCandidate] = {}
    for match in _HEADCOUNT.finditer(text):
        sentence = _sentence(text, match.start(), match.end())
        if _TAG_SOUP.search(sentence):
            continue
        raw_digits = match.group(1) or match.group(3) or match.group(5)
        raw_scale = match.group(2) or match.group(4) or match.group(6)
        value = _parse_headcount_value(raw_digits, raw_scale)
        if value is None or not _PLAUSIBLE[0] <= value <= _PLAUSIBLE[1]:
            continue
        as_of = _AS_OF.search(sentence) or _AS_OF.search(text[max(0, match.start() - 220) : match.end() + 80])
        clause_tail = text[match.start() : match.end() + 120]
        clause_end = _CLAUSE_END.search(clause_tail, match.end() - match.start())
        clause = clause_tail[: clause_end.start()] if clause_end else clause_tail
        sentence_prefix = _SENTENCE_END.split(text[max(0, match.start() - 60) : match.start()])[-1]
        key = (value, as_of.group(1) if as_of else None, sentence)
        found.setdefault(key, FilingCandidate(value, key[1], sentence, bool(_PARTIAL.search(sentence_prefix + clause))))
    return list(found.values())


def _shared_candidates(totals: Sequence[FilingCandidate]) -> list[extraction_primitive.Candidate]:
    return [extraction_primitive.Candidate(value=c.value, sentence=c.sentence, as_of=c.as_of) for c in totals]


def _chosen_total(totals: Sequence[FilingCandidate], selection: extraction_primitive.Selection) -> FilingCandidate:
    return next(c for c in totals if c.value == selection.value)


def select_total(found: list[FilingCandidate]) -> tuple[ExtractionStatus, FilingCandidate | None]:
    """Delegate deterministic single-candidate selection to `factors.shared.extraction.extract_metric`."""
    totals = [candidate for candidate in found if not candidate.partial]
    if not totals:
        return "no_candidate", None
    selection = extraction_primitive.extract_metric(_shared_candidates(totals)).selection
    if selection is None:
        return "needs_model_selection", None
    return "resolved", _chosen_total(totals, selection)


def parse_as_of(text: str | None) -> date | None:
    if text is None:
        return None
    for pattern in ("%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _fetch_bytes_via_gateway(
    http: Any, gateway_instance: SourceGateway | None, endpoint: str, url: str, *, key: FetchKey
) -> bytes:
    from data_engine.sources import gateway as gw_mod

    def fetch(identity: FetchKey) -> bytes:
        if gateway_instance is not None:
            return gateway_instance.call("sec", endpoint, lambda: _get_bytes(http, url), identity=identity)
        response = gw_mod.http_get(
            http, "sec", endpoint, url, identity=identity, caller="filing_extraction.latest_periodic_filing"
        )
        response.raise_for_status()
        return bytes(response.content)

    return gw_mod.fetch_once(key, fetch, content_type="application/octet-stream")


def _fetch_json_via_gateway(
    http: Any, gateway_instance: SourceGateway | None, endpoint: str, url: str, *, key: FetchKey
) -> dict[str, Any]:
    return dict(json.loads(_fetch_bytes_via_gateway(http, gateway_instance, endpoint, url, key=key).decode()))


def _annual_filing_from_index(
    cik: int,
    index_block: dict[str, Any],
    *,
    cutoff_date: date,
    http: Any,
    gateway: SourceGateway | None = None,
    allowed_forms: Collection[str] = ANNUAL_FORMS,
) -> FilingDocument | None:
    forms = index_block.get("form") or ()
    for index, form in enumerate(forms):
        if form not in allowed_forms:
            continue
        filing_date = date.fromisoformat(index_block["filingDate"][index])
        if filing_date > cutoff_date:
            continue
        accession = index_block["accessionNumber"][index].replace("-", "")
        document = index_block["primaryDocument"][index]
        url = ARCHIVE_URL.format(cik=cik, accession=accession, document=document)
        body = _fetch_bytes_via_gateway(
            http,
            gateway,
            "archive",
            url,
            key=FetchKey("sec", "archive", f"filing:{cik:010d}:{accession}:{document}", ACCESSION_AS_OF),
        )
        return FilingDocument(cik, accession, form, filing_date, document, url, body)
    return None


def _latest_filing_for_forms(
    cik: int,
    *,
    http: Any,
    gateway: SourceGateway | None = None,
    cutoff: datetime,
    allowed_forms: Collection[str],
) -> FilingDocument | None:
    cutoff_date = cutoff.astimezone(UTC).date()
    submissions = _fetch_json_via_gateway(
        http, gateway, "submissions", SUBMISSIONS_URL.format(cik=cik), key=submissions_key(cik)
    )
    filings = submissions.get("filings") or {}
    found = _annual_filing_from_index(
        cik,
        filings.get("recent") or {},
        cutoff_date=cutoff_date,
        http=http,
        gateway=gateway,
        allowed_forms=allowed_forms,
    )
    if found is not None:
        return found
    for file_meta in filings.get("files") or ():
        name = file_meta.get("name")
        if not name:
            continue
        filing_from = file_meta.get("filingFrom")
        if filing_from:
            try:
                if date.fromisoformat(str(filing_from)) > cutoff_date:
                    continue
            except ValueError:
                pass
        overflow = _fetch_json_via_gateway(
            http,
            gateway,
            "submissions",
            SUBMISSIONS_OVERFLOW_URL.format(name=name),
            key=submissions_key(cik, overflow=name),
        )
        found = _annual_filing_from_index(
            cik, overflow, cutoff_date=cutoff_date, http=http, gateway=gateway, allowed_forms=allowed_forms
        )
        if found is not None:
            return found
    return None


def latest_annual_filing(
    cik: int, *, http: Any, gateway: SourceGateway | None = None, cutoff: datetime
) -> FilingDocument | None:
    return _latest_filing_for_forms(cik, http=http, gateway=gateway, cutoff=cutoff, allowed_forms=ANNUAL_FORMS)


def latest_periodic_filing(
    cik: int, *, http: Any, gateway: SourceGateway | None = None, cutoff: datetime
) -> FilingDocument | None:
    return _latest_filing_for_forms(cik, http=http, gateway=gateway, cutoff=cutoff, allowed_forms=STATEMENT_FORMS)


def format_accession(accession: str) -> str:
    digits = accession.replace("-", "")
    return f"{digits[:10]}-{digits[10:12]}-{digits[12:]}" if len(digits) == 18 and digits.isdigit() else accession


_CLASS_OF_STOCK_AXIS = "us-gaap:StatementClassOfStockAxis"
_DEI_SHARES_CONCEPT = "dei:EntityCommonStockSharesOutstanding"
_DEI_TRADING_SYMBOL_CONCEPT = "dei:TradingSymbol"
_PAR_VALUE_CONCEPT = "us-gaap:CommonStockParOrStatedValuePerShare"
_CONVERSION_RATE_CONCEPT = re.compile(
    r"name\s*=\s*[\"']([a-z0-9_-]+:CommonStockConversionRate)[\"']",
    re.IGNORECASE,
)


def _uniform_class_par_values(ix: Any, members: Sequence[str]) -> bool:
    par_by_member: dict[str, Decimal] = {}
    for fact in ix.facts((_PAR_VALUE_CONCEPT,)):
        if fact.value is None or fact.context.start is not None:
            continue
        member = dict(fact.context.dimensions).get(_CLASS_OF_STOCK_AXIS)
        if isinstance(member, str) and member in members:
            par_by_member[member] = fact.value
    return len(set(par_by_member.values())) <= 1


def _listed_stock_members(ix: Any, members: Sequence[str]) -> set[str]:
    listed: set[str] = set()
    for text_fact in ix.text_facts((_DEI_TRADING_SYMBOL_CONCEPT,), limit=40):
        context = ix.contexts.get(text_fact.context_id)
        if context is not None:
            member = dict(context.dimensions).get(_CLASS_OF_STOCK_AXIS)
            if isinstance(member, str) and member in members:
                listed.add(member)
    return listed


def _class_conversion_rates(ix: Any, body: bytes, members: Sequence[str], *, as_of: date) -> dict[str, Decimal] | None:
    concepts = set(_CONVERSION_RATE_CONCEPT.findall(body.decode("utf-8", "ignore")))
    latest_by_member: dict[str, tuple[date, Decimal]] = {}
    for fact in ix.facts(concepts) if concepts else ():
        if fact.value is None or fact.context.start is not None or fact.context.end > as_of:
            continue
        dims = dict(fact.context.dimensions)
        member = dims.get(_CLASS_OF_STOCK_AXIS) if len(dims) == 1 else None
        if (
            isinstance(member, str)
            and member in members
            and (member not in latest_by_member or fact.context.end >= latest_by_member[member][0])
        ):
            latest_by_member[member] = (fact.context.end, fact.value)
    base_candidates = sorted(_listed_stock_members(ix, members)) or [
        member
        for member in members
        if latest_by_member.get(member, (as_of, Decimal(-1)))[1] in (Decimal(0), Decimal(1))
    ]
    if len(base_candidates) != 1:
        return None
    base_member = base_candidates[0]
    rates: dict[str, Decimal] = {base_member: Decimal(1)}
    for member in members:
        if member == base_member:
            continue
        tagged = latest_by_member.get(member)
        if tagged is None or tagged[1] <= 0:
            return None
        rates[member] = tagged[1]
    return rates


def _share_entry(
    *, end: str, val: Decimal, accn: str, fy: int, fp: str, form: str, filed: str, segment: str | None = None
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "end": end,
        "val": int(val) if val == int(val) else str(val),
        "accn": accn,
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed": filed,
    }
    if segment is not None:
        entry["segment"] = segment
    return entry


def multi_class_share_entries(document: FilingDocument) -> list[dict[str, Any]]:
    """Extract dimensional cover-page share entries from an iXBRL filing document (#1265, #1519)."""
    from data_engine.datahub.standards.inline_xbrl import InlineXbrl

    ix = InlineXbrl(document.body)
    by_end: dict[date, dict[str, Decimal]] = {}
    for fact in ix.facts((_DEI_SHARES_CONCEPT,)):
        if fact.value is None or fact.context.start is not None:
            continue
        dims = dict(fact.context.dimensions)
        if len(dims) == 1 and _CLASS_OF_STOCK_AXIS in dims:
            by_end.setdefault(fact.context.end, {})[dims[_CLASS_OF_STOCK_AXIS]] = fact.value
    if not by_end:
        return []
    latest_end = max(by_end)
    by_member = by_end[latest_end]
    if len(by_member) < 2 or not _uniform_class_par_values(ix, tuple(by_member)):
        return []
    base_kwargs: dict[str, Any] = {
        "end": latest_end.isoformat(),
        "accn": format_accession(document.accession),
        "fy": latest_end.year,
        "fp": "FY" if document.form in ANNUAL_STATEMENT_FORMS else "Q",
        "form": document.form,
        "filed": document.filing_date.isoformat(),
    }
    rates = _class_conversion_rates(ix, document.body, tuple(by_member), as_of=latest_end)
    if rates is not None:
        total = sum((count * rates[member] for member, count in by_member.items()), Decimal(0))
        return [_share_entry(val=total, **base_kwargs)]
    return [_share_entry(val=val, segment=member, **base_kwargs) for member, val in sorted(by_member.items())]


def _get_bytes(http: Any, url: str) -> bytes:
    response = http.get(url)
    response.raise_for_status()
    return bytes(response.content)


def fetch_annual_filing(
    cik: int, *, http: Any, gateway: SourceGateway, cutoff: datetime
) -> FilingDocument | None | ExtractionOutcome:
    try:
        return latest_annual_filing(cik, http=http, gateway=gateway, cutoff=cutoff)
    except CapacityExceeded as error:
        return ExtractionOutcome(cik, "deferred_capacity", detail=str(error))
    except Exception as error:  # noqa: BLE001
        return ExtractionOutcome(cik, "error", detail=f"{type(error).__name__}: {error}")


def _model_selection_summary(
    model_runs: Sequence[llm.ModelSelection], status: ExtractionStatus
) -> tuple[str, bool | None]:
    if not model_runs:
        return "", None
    (model_run,) = model_runs
    detail = f" model={model_run.model} invocation={model_run.invocation_id}" + (
        " (replayed)" if model_run.replayed else ""
    )
    if status == "model_declined":
        detail += f" reason={model_run.reason!r}"
    return detail, model_run.replayed


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
    record_cik: int | None = None,
    select_with_model: bool = True,
    model_transport: llm.Transport | None = None,
    issuer_label: str | None = None,
) -> ExtractionOutcome:
    """Enumerate filing headcount candidates, select by deterministic rule or model, and persist."""
    record_cik = cik if record_cik is None else record_cik
    document = fetch_annual_filing(cik, http=http, gateway=gateway, cutoff=cutoff)
    if isinstance(document, ExtractionOutcome):
        return document
    if document is None:
        return ExtractionOutcome(cik, "no_annual_filing", detail="no 10-K/20-F on file at the cutoff")

    try:
        found = candidates(filing_plain_text(document.body))
    except Exception as error:  # noqa: BLE001
        return ExtractionOutcome(cik, "error", detail=f"{type(error).__name__}: {error}")
    totals = [c for c in found if not c.partial]
    model_runs: list[llm.ModelSelection] = []
    selector: extraction_primitive.Selector | None = None
    if select_with_model and llm.is_configured():
        selector = llm.as_selector(
            connection if write else None,
            cik=cik,
            accession=document.accession,
            form=document.form,
            issuer_label=issuer_label or f"CIK {cik}",
            caller=f"standard-backfill:{standard.metric}",
            standard=standard.metric,
            persist=write,
            transport=model_transport,
            on_model_selection=model_runs.append,
        )
    selection = extraction_primitive.extract_metric(_shared_candidates(totals), selector).selection
    status: ExtractionStatus
    if selection is not None:
        status = "resolved"
    elif not totals:
        status = "no_candidate"
    elif selector is not None:
        status = "model_declined"
    else:
        status = "needs_model_selection"
    chosen = _chosen_total(totals, selection) if selection is not None else None
    extractor = selection.extractor if selection is not None else RULE_SINGLE_CANDIDATE
    model_detail, model_replayed = _model_selection_summary(model_runs, status)
    outcome = ExtractionOutcome(
        cik,
        status,
        extractor=extractor,
        value=chosen.value if chosen else None,
        as_of=parse_as_of(chosen.as_of) if chosen else None,
        sentence=chosen.sentence if chosen else None,
        candidates=tuple(found),
        accession=document.accession,
        form=document.form,
        filing_date=document.filing_date,
        detail=f"{len(found)} candidate(s), {len({c.value for c in totals})} distinct total(s){model_detail}",
        model_replayed=model_replayed,
    )
    if status != "resolved" or not write or chosen is None:
        return outcome
    if _already_recorded(connection, record_cik, document.accession):
        return ExtractionOutcome(
            record_cik,
            "already_recorded",
            accession=document.accession,
            value=chosen.value,
            model_replayed=model_replayed,
        )

    landed_at = now or datetime.now(UTC)
    raw_id = insert_fetch(
        connection,
        source=DataSource.SEC,
        source_record_id=f"filing-document:CIK{cik:010d}:{document.accession}:{document.primary_document}",
        body=document.body,
        content_type="text/html",
        fetched_at=landed_at,
        source_published_at=knowable_instant(AsOfAxis.ACCESSION, document.filing_date),
        metadata={"form": document.form, "accession": document.accession, "cik": cik, "url": document.url},
        store=store,
        recorded_at=landed_at,
    )
    filing_cik = "" if record_cik == cik else f" filing_cik={cik}"
    evidence_ref = (
        f"accession={document.accession} form={document.form} filed={document.filing_date.isoformat()}{filing_cik} "
        f"raw=raw.fetches:{raw_id} extractor={extractor}{model_detail} "
        f"span={chosen.sentence[:400]!r}"
    )
    fact_id = record_headcount(
        connection,
        cik=record_cik,
        headcount=Decimal(chosen.value),
        knowable_at=knowable_instant(AsOfAxis.ACCESSION, document.filing_date),
        period_end=outcome.as_of,
        source=EXTRACTION_SOURCE,
        evidence_ref=evidence_ref,
        confidence=confidence_for(standard.confidence_policy_id, extractor),
    )
    return replace(outcome, cik=record_cik, raw_fetch_id=raw_id, fact_id=fact_id)


def _already_recorded(connection: Any, cik: int, accession: str) -> bool:
    row = connection.execute(
        "select 1 from staging.issuer_headcount_facts where cik = %s and source = %s and evidence_ref like %s limit 1",
        (cik, EXTRACTION_SOURCE, f"accession={accession} %"),
    ).fetchone()
    return row is not None
