"""Network-backed TOPT source assets other than baseline identity."""

from __future__ import annotations

import re
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from decimal import Decimal

from truealpha_contracts import DataDomain, DataSource

from data_engine import jsonable, raw_store
from data_engine.capture import source_results
from data_engine.capture.topt import TOPT_INSTRUMENTS, ToptInstrument
from data_engine.normalizers import moomoo as moomoo_normalizer
from data_engine.normalizers import sec_companyfacts, sec_filings, yahoo_chart
from data_engine.sources import moomoo as mm
from data_engine.sources import yahoo
from data_engine.sources.sec import COMPANY_FACTS_URL
from data_engine.sources.sec import client as sec_client

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/index.json"


def _safe_source_error(error: Exception) -> str:
    return f"{type(error).__name__}: source request failed"


def _requirement(scope, subject_id: str, domain: DataDomain):
    matches = [
        requirement
        for requirement in scope.requirements
        if requirement.subject_id == subject_id and requirement.domain is domain
    ]
    if len(matches) != 1:
        raise ValueError(f"scope must contain exactly one {subject_id}/{domain.value} requirement")
    return matches[0]


def capture_sec_financials(conn, *, run_id: str, scope, attempt: int = 0) -> tuple[int, ...]:
    ids: list[int] = []
    seen_issuers: set[str] = set()
    with sec_client() as client:
        for instrument in TOPT_INSTRUMENTS:
            issuer_id = instrument.issuer_id
            if issuer_id in seen_issuers:
                continue
            seen_issuers.add(issuer_id)
            response = client.get(COMPANY_FACTS_URL.format(cik=instrument.issuer_cik))
            response.raise_for_status()
            fetched_at = datetime.now(UTC)
            raw_id = raw_store.insert_fetch(
                conn,
                source=DataSource.SEC,
                source_record_id=f"companyfacts:CIK{instrument.issuer_cik:010d}",
                body=response.content,
                content_type="application/json",
                fetched_at=fetched_at,
            )
            record_ids = sec_companyfacts.normalize_fetch(
                conn,
                raw_fetch_id=raw_id,
                issuer_id=issuer_id,
                issuer_category="financial" if instrument.issuer_cik == 19617 else "non_financial",
            )
            rows = conn.execute(
                """
                select metric, transaction_time from staging.financial_facts
                where id = any(%s) order by transaction_time
                """,
                (list(record_ids),),
            ).fetchall()
            metrics = {row[0] for row in rows}
            requirement = _requirement(scope, issuer_id, DataDomain.FINANCIAL_FACTS)
            ids.append(
                source_results.put(
                    conn,
                    source_results.CaptureSourceResult(
                        run_id=run_id,
                        subject_id=issuer_id,
                        domain=DataDomain.FINANCIAL_FACTS,
                        partition_key=requirement.partition_key,
                        source=DataSource.SEC,
                        outcome=source_results.SourceResultOutcome.SUCCESS,
                        raw_refs=(raw_store.raw_ref(raw_id),),
                        domain_record_ids=tuple(f"staging.financial_facts:{record_id}" for record_id in record_ids),
                        observed_fields=tuple(sorted({*metrics, "fiscal_period", "unit"})),
                        min_knowable_at=rows[0][1] if rows else None,
                        max_knowable_at=rows[-1][1] if rows else None,
                        observed_at=fetched_at,
                        confidence=Decimal("1"),
                        mapping_version=sec_companyfacts.MAPPING_VERSION,
                        attempt=attempt,
                    ),
                )
            )
            conn.commit()
    return tuple(ids)


def capture_yahoo_prices(conn, *, run_id: str, scope, period_days: int = 365, attempt: int = 0) -> tuple[int, ...]:
    ids: list[int] = []
    for instrument in TOPT_INSTRUMENTS:
        response = yahoo.fetch_chart_response(instrument.ticker, period_days=period_days)
        fetched_at = datetime.now(UTC)
        raw_id = raw_store.insert_fetch(
            conn,
            source=DataSource.YAHOO,
            source_record_id=f"chart:{instrument.ticker}:{period_days}d:{fetched_at.date().isoformat()}",
            body=response.content,
            content_type="application/json",
            fetched_at=fetched_at,
            metadata={"symbol": instrument.ticker, "period_days": period_days, "events": ["div", "splits"]},
        )
        listing_id = f"listing:vendor:{instrument.moomoo_code}"
        price_ids, action_ids = yahoo_chart.normalize_fetch(
            conn,
            raw_fetch_id=raw_id,
            issuer_id=instrument.issuer_id,
            instrument_id=instrument.instrument_id,
            listing_id=listing_id,
            symbol=instrument.ticker,
        )
        price_times = conn.execute(
            "select transaction_time from staging.market_prices where id = any(%s) order by transaction_time",
            (list(price_ids),),
        ).fetchall()
        if not price_times:
            raise ValueError(f"Yahoo returned no closed daily bars for {instrument.ticker}")
        price_requirement = _requirement(scope, instrument.instrument_id, DataDomain.MARKET_PRICES)
        ids.append(
            source_results.put(
                conn,
                source_results.CaptureSourceResult(
                    run_id=run_id,
                    subject_id=instrument.instrument_id,
                    domain=DataDomain.MARKET_PRICES,
                    partition_key=price_requirement.partition_key,
                    source=DataSource.YAHOO,
                    outcome=source_results.SourceResultOutcome.SUCCESS,
                    raw_refs=(raw_store.raw_ref(raw_id),),
                    domain_record_ids=tuple(f"staging.market_prices:{record_id}" for record_id in price_ids),
                    observed_fields=("open", "high", "low", "close", "adjusted_close", "volume", "currency"),
                    min_knowable_at=price_times[0][0],
                    max_knowable_at=price_times[-1][0],
                    observed_at=fetched_at,
                    confidence=yahoo_chart.PRICE_CONFIDENCE,
                    mapping_version=yahoo_chart.MAPPING_VERSION,
                    attempt=attempt,
                ),
            )
        )
        action_times = conn.execute(
            "select transaction_time from staging.corporate_actions where id = any(%s) order by transaction_time",
            (list(action_ids),),
        ).fetchall()
        action_requirement = _requirement(scope, instrument.instrument_id, DataDomain.CORPORATE_ACTIONS)
        ids.append(
            source_results.put(
                conn,
                source_results.CaptureSourceResult(
                    run_id=run_id,
                    subject_id=instrument.instrument_id,
                    domain=DataDomain.CORPORATE_ACTIONS,
                    partition_key=action_requirement.partition_key,
                    source=DataSource.YAHOO,
                    outcome=source_results.SourceResultOutcome.SUCCESS,
                    raw_refs=(raw_store.raw_ref(raw_id),),
                    domain_record_ids=tuple(f"staging.corporate_actions:{record_id}" for record_id in action_ids),
                    observed_fields=action_requirement.required_fields if action_ids else (),
                    min_knowable_at=action_times[0][0] if action_times else None,
                    max_knowable_at=action_times[-1][0] if action_times else None,
                    observed_at=fetched_at,
                    confidence=yahoo_chart.ACTION_CONFIDENCE,
                    mapping_version=yahoo_chart.MAPPING_VERSION,
                    attempt=attempt,
                ),
            )
        )
        conn.commit()
    return tuple(ids)


def _recent_filings(payload) -> list[dict]:
    recent = payload.get("filings", {}).get("recent")
    if not isinstance(recent, dict):
        raise ValueError("SEC submissions payload lost filings.recent")
    required = ("form", "accessionNumber", "primaryDocument", "filingDate", "reportDate", "items")
    if any(not isinstance(recent.get(field), list) for field in required):
        raise ValueError("SEC submissions recent filing arrays drifted")
    count = len(recent["form"])
    if any(len(recent[field]) != count for field in required):
        raise ValueError("SEC submissions recent filing arrays have different lengths")
    return [{field: recent[field][index] for field in required} for index in range(count)]


def _filing_knowable_at(filing_date: str) -> datetime:
    # Submissions provides only a date in the stable recent arrays. End-of-day
    # UTC is conservative and cannot expose an item before its acceptance.
    return datetime.combine(date.fromisoformat(filing_date) + timedelta(days=1), datetime_time.min, tzinfo=UTC)


def capture_sec_filings(conn, *, run_id: str, scope, attempt: int = 0) -> tuple[int, ...]:
    ids: list[int] = []
    seen_issuers: set[str] = set()
    with sec_client() as client:
        for instrument in TOPT_INSTRUMENTS:
            issuer_id = instrument.issuer_id
            if issuer_id in seen_issuers:
                continue
            seen_issuers.add(issuer_id)
            response = client.get(SUBMISSIONS_URL.format(cik=instrument.issuer_cik))
            response.raise_for_status()
            observed_at = datetime.now(UTC)
            submissions_raw_id = raw_store.insert_fetch(
                conn,
                source=DataSource.SEC,
                source_record_id=f"submissions:CIK{instrument.issuer_cik:010d}",
                body=response.content,
                content_type="application/json",
                fetched_at=observed_at,
            )
            filings = _recent_filings(response.json())
            annual = next((filing for filing in filings if filing["form"] in {"10-K", "20-F", "40-F"}), None)
            earnings = next(
                (filing for filing in filings if filing["form"] == "8-K" and "2.02" in str(filing.get("items") or "")),
                None,
            )
            if earnings is None:
                earnings = next((filing for filing in filings if filing["form"] == "8-K"), None)
            selected: list[dict] = []
            for filing in (annual, earnings):
                if filing is not None and filing["accessionNumber"] not in {
                    selected_filing["accessionNumber"] for selected_filing in selected
                }:
                    selected.append(filing)

            raw_refs = [raw_store.raw_ref(submissions_raw_id)]
            document_ids: list[str] = []
            extraction_ids: list[int] = []
            knowable_times: list[datetime] = []
            guidance_signals: list[str] = []
            for filing in selected:
                accession = str(filing["accessionNumber"])
                path_accession = accession.replace("-", "")
                index_response = client.get(INDEX_URL.format(cik=instrument.issuer_cik, accession=path_accession))
                index_response.raise_for_status()
                index_fetched_at = datetime.now(UTC)
                index_raw_id = raw_store.insert_fetch(
                    conn,
                    source=DataSource.SEC,
                    source_record_id=f"filing-index:{accession}",
                    body=index_response.content,
                    content_type="application/json",
                    fetched_at=index_fetched_at,
                    source_published_at=_filing_knowable_at(str(filing["filingDate"])),
                )
                raw_refs.append(raw_store.raw_ref(index_raw_id))
                primary = str(filing["primaryDocument"])
                document_names = [primary]
                if filing["form"] == "8-K":
                    index_items = index_response.json().get("directory", {}).get("item", [])
                    exhibit_names = [
                        str(item.get("name"))
                        for item in index_items
                        if isinstance(item, dict)
                        and str(item.get("name", "")).lower().endswith((".htm", ".html"))
                        and re.search(r"(?:ex|exhibit)[-_]?99|99[-_.]?1", str(item.get("name", "")), re.I)
                    ]
                    document_names.extend(name for name in exhibit_names[:3] if name != primary)

                knowable_at = _filing_knowable_at(str(filing["filingDate"]))
                knowable_times.append(knowable_at)
                report_date = date.fromisoformat(filing["reportDate"]) if filing.get("reportDate") else None
                for document_name in document_names:
                    source_url = ARCHIVE_URL.format(
                        cik=instrument.issuer_cik,
                        accession=path_accession,
                        document=document_name,
                    )
                    document_response = client.get(source_url)
                    document_response.raise_for_status()
                    document_fetched_at = datetime.now(UTC)
                    document_raw_id = raw_store.insert_fetch(
                        conn,
                        source=DataSource.SEC,
                        source_record_id=f"filing:{accession}:{document_name}",
                        body=document_response.content,
                        content_type=document_response.headers.get("content-type", "text/html").split(";")[0],
                        fetched_at=document_fetched_at,
                        source_published_at=knowable_at,
                        metadata={"cik": instrument.issuer_cik, "accession": accession, "form": filing["form"]},
                    )
                    raw_refs.append(raw_store.raw_ref(document_raw_id))
                    document_id, extraction_id = sec_filings.normalize_document(
                        conn,
                        raw_fetch_id=document_raw_id,
                        issuer_id=issuer_id,
                        accession=accession,
                        form=str(filing["form"]),
                        filing_period=report_date,
                        document_name=document_name,
                        source_url=source_url,
                        knowable_at=knowable_at,
                    )
                    document_ids.append(f"staging.filing_documents:{document_id}")
                    extraction_ids.append(extraction_id)
                    lower_text = document_response.text.lower()
                    if "guidance" in lower_text or "outlook" in lower_text:
                        guidance_signals.append(f"{accession}/{document_name}")
                time.sleep(0.25)

            observed_at = datetime.now(UTC)
            filing_requirement = _requirement(scope, issuer_id, DataDomain.FILINGS)
            filing_success = bool(document_ids)
            ids.append(
                source_results.put(
                    conn,
                    source_results.CaptureSourceResult(
                        run_id=run_id,
                        subject_id=issuer_id,
                        domain=DataDomain.FILINGS,
                        partition_key=filing_requirement.partition_key,
                        source=DataSource.SEC,
                        outcome=(
                            source_results.SourceResultOutcome.SUCCESS
                            if filing_success
                            else source_results.SourceResultOutcome.FAILED
                        ),
                        raw_refs=tuple(raw_refs),
                        domain_record_ids=tuple(document_ids),
                        observed_fields=filing_requirement.required_fields if filing_success else (),
                        min_knowable_at=min(knowable_times) if knowable_times else None,
                        max_knowable_at=max(knowable_times) if knowable_times else None,
                        observed_at=observed_at,
                        confidence=Decimal("1"),
                        mapping_version=sec_filings.MAPPING_VERSION,
                        attempt=attempt,
                        detail=None if filing_success else "No annual or current-report filing was selected.",
                    ),
                )
            )
            extraction_requirement = _requirement(scope, issuer_id, DataDomain.FILING_EXTRACTIONS)
            semantic_extraction_ids = sec_filings.accepted_semantic_extraction_ids(conn, extraction_ids)
            ids.append(
                source_results.put(
                    conn,
                    source_results.CaptureSourceResult(
                        run_id=run_id,
                        subject_id=issuer_id,
                        domain=DataDomain.FILING_EXTRACTIONS,
                        partition_key=extraction_requirement.partition_key,
                        source=DataSource.SEC,
                        outcome=(
                            source_results.SourceResultOutcome.SUCCESS
                            if semantic_extraction_ids
                            else source_results.SourceResultOutcome.FAILED
                        ),
                        raw_refs=tuple(raw_refs),
                        domain_record_ids=tuple(
                            f"staging.filing_extractions:{extraction_id}" for extraction_id in semantic_extraction_ids
                        ),
                        observed_fields=(extraction_requirement.required_fields if semantic_extraction_ids else ()),
                        min_knowable_at=min(knowable_times) if knowable_times else None,
                        max_knowable_at=max(knowable_times) if knowable_times else None,
                        observed_at=observed_at,
                        confidence=Decimal("1") if semantic_extraction_ids else Decimal("0"),
                        mapping_version=sec_filings.MAPPING_VERSION,
                        attempt=attempt,
                        detail=(
                            None
                            if semantic_extraction_ids
                            else f"{len(extraction_ids)} document envelopes contain no accepted semantic claims."
                        ),
                    ),
                )
            )
            guidance_requirement = _requirement(scope, issuer_id, DataDomain.COMPANY_GUIDANCE)
            ids.append(
                source_results.put(
                    conn,
                    source_results.CaptureSourceResult(
                        run_id=run_id,
                        subject_id=issuer_id,
                        domain=DataDomain.COMPANY_GUIDANCE,
                        partition_key=guidance_requirement.partition_key,
                        source=DataSource.SEC,
                        outcome=source_results.SourceResultOutcome.FAILED,
                        raw_refs=tuple(raw_refs),
                        domain_record_ids=(),
                        observed_fields=(),
                        min_knowable_at=None,
                        max_knowable_at=None,
                        observed_at=observed_at,
                        confidence=Decimal("0"),
                        mapping_version=sec_filings.MAPPING_VERSION,
                        attempt=attempt,
                        detail=(
                            "Guidance/outlook text requires structured range extraction in: "
                            + ", ".join(guidance_signals)
                            if guidance_signals
                            else "No accepted structured guidance extractor ran; keyword absence cannot certify an empty result."
                        ),
                    ),
                )
            )
            conn.commit()
    return tuple(ids)


def capture_moomoo_domains(conn, *, run_id: str, scope, attempt: int = 0) -> tuple[int, ...]:
    ids: list[int] = []
    issuer_instruments: dict[str, ToptInstrument] = {}
    for instrument in TOPT_INSTRUMENTS:
        issuer_instruments.setdefault(instrument.issuer_id, instrument)

    def persist(code: str, endpoint: str, payload, observed_at: datetime) -> int:
        return raw_store.insert_json_fetch(
            conn,
            source=DataSource.MOOMOO,
            source_record_id=f"{endpoint}:{code}",
            payload=jsonable.to_jsonable(payload),
            fetched_at=observed_at,
        )

    with mm.connect() as context:
        for issuer_id, instrument in issuer_instruments.items():
            code = instrument.moomoo_code
            endpoint_specs = (
                (
                    "analyst_consensus",
                    DataDomain.FORECASTS,
                    lambda: mm.get_analyst_consensus(context, code, caller="topt_capture"),
                    moomoo_normalizer.normalize_consensus,
                    ("metric", "forecast_period", "estimate", "currency", "knowable_at"),
                    "forecast_facts",
                ),
                (
                    "rating_summary",
                    DataDomain.ANALYST_RATINGS,
                    lambda: mm.get_rating_summary(context, code, analyst_dimension=True, num=20, caller="topt_capture"),
                    moomoo_normalizer.normalize_ratings,
                    ("analyst_id", "action", "rating", "recommendation_at", "knowable_at"),
                    "analyst_rating_events",
                ),
                (
                    "financials_revenue_breakdown",
                    DataDomain.SEGMENTS,
                    lambda: mm.get_financials_revenue_breakdown(context, code, caller="topt_capture"),
                    moomoo_normalizer.normalize_segments,
                    ("segment", "revenue", "period", "taxonomy_version"),
                    "segment_facts",
                ),
            )
            for endpoint, domain, call, normalizer, fields, table in endpoint_specs:
                try:
                    payload = call()
                    observed_at = datetime.now(UTC)
                    raw_id = persist(code, endpoint, payload, observed_at)
                    record_ids = normalizer(conn, raw_fetch_id=raw_id, issuer_id=issuer_id)
                    outcome = source_results.SourceResultOutcome.SUCCESS
                    detail = None
                except mm.MoomooConnectionError as error:
                    observed_at = datetime.now(UTC)
                    detail = _safe_source_error(error)
                    raw_id = persist(code, f"error:{endpoint}", {"error": detail}, observed_at)
                    record_ids = ()
                    outcome = source_results.SourceResultOutcome.FAILED
                requirement = _requirement(scope, issuer_id, domain)
                times = (
                    conn.execute(
                        f"select transaction_time from staging.{table} where id = any(%s) order by transaction_time",
                        (list(record_ids),),
                    ).fetchall()
                    if record_ids
                    else []
                )
                ids.append(
                    source_results.put(
                        conn,
                        source_results.CaptureSourceResult(
                            run_id=run_id,
                            subject_id=issuer_id,
                            domain=domain,
                            partition_key=requirement.partition_key,
                            source=DataSource.MOOMOO,
                            outcome=outcome,
                            raw_refs=(raw_store.raw_ref(raw_id),),
                            domain_record_ids=tuple(f"staging.{table}:{record_id}" for record_id in record_ids),
                            observed_fields=fields if record_ids else (),
                            min_knowable_at=times[0][0] if times else None,
                            max_knowable_at=times[-1][0] if times else None,
                            observed_at=observed_at,
                            confidence=moomoo_normalizer.CONFIDENCE,
                            mapping_version=moomoo_normalizer.MAPPING_VERSION,
                            attempt=attempt,
                            detail=detail,
                        ),
                    )
                )
                conn.commit()

        for instrument in TOPT_INSTRUMENTS:
            code = instrument.moomoo_code
            try:
                payload = mm.get_corporate_actions_dividends(context, code, caller="topt_capture")
                observed_at = datetime.now(UTC)
                raw_id = persist(code, "dividends", payload, observed_at)
                record_ids = moomoo_normalizer.normalize_dividends(
                    conn,
                    raw_fetch_id=raw_id,
                    instrument_id=instrument.instrument_id,
                    listing_id=f"listing:vendor:{code}",
                )
                outcome = source_results.SourceResultOutcome.SUCCESS
                detail = None
            except mm.MoomooConnectionError as error:
                observed_at = datetime.now(UTC)
                detail = _safe_source_error(error)
                raw_id = persist(code, "error:dividends", {"error": detail}, observed_at)
                record_ids = ()
                outcome = source_results.SourceResultOutcome.FAILED
            requirement = _requirement(scope, instrument.instrument_id, DataDomain.CORPORATE_ACTIONS)
            times = (
                conn.execute(
                    "select transaction_time from staging.corporate_actions where id = any(%s) order by transaction_time",
                    (list(record_ids),),
                ).fetchall()
                if record_ids
                else []
            )
            ids.append(
                source_results.put(
                    conn,
                    source_results.CaptureSourceResult(
                        run_id=run_id,
                        subject_id=instrument.instrument_id,
                        domain=DataDomain.CORPORATE_ACTIONS,
                        partition_key=requirement.partition_key,
                        source=DataSource.MOOMOO,
                        outcome=outcome,
                        raw_refs=(raw_store.raw_ref(raw_id),),
                        domain_record_ids=tuple(f"staging.corporate_actions:{record_id}" for record_id in record_ids),
                        observed_fields=requirement.required_fields if record_ids else (),
                        min_knowable_at=times[0][0] if times else None,
                        max_knowable_at=times[-1][0] if times else None,
                        observed_at=observed_at,
                        confidence=moomoo_normalizer.ACTION_CONFIDENCE,
                        mapping_version=moomoo_normalizer.MAPPING_VERSION,
                        attempt=attempt,
                        detail=detail,
                    ),
                )
            )
            conn.commit()
    return tuple(ids)
