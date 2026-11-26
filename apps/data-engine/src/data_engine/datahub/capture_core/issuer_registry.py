"""Issuer registry metadata and operating branch classification."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx
from factors.snapshot_inputs import OperatingBranch

from data_engine.sources import sec
from data_engine.sources.gateway import CapacityExceeded

_DEPOSITORY_INSTITUTION_SIC = range(6000, 6100)
_INSURANCE_SIC = range(6300, 6400)

ANNUAL_STATEMENT_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"})
INTERIM_STATEMENT_FORMS = frozenset({"10-Q", "10-Q/A", "10-QT", "10-QT/A"})
STATEMENT_FORMS = ANNUAL_STATEMENT_FORMS | INTERIM_STATEMENT_FORMS

REGISTRY_ATTEMPTS = 3
REGISTRY_BACKOFF_SECONDS = 1.0
REGISTRY_OUTAGE_AFTER = 3
REGISTRY_READ_TIMEOUT_SECONDS = 10.0
UNAVAILABLE_PAYLOAD_KEY = "registry_unavailable"

logger = logging.getLogger(__name__)
_TRANSIENT = (httpx.TransportError, httpx.DecodingError, json.JSONDecodeError, UnicodeDecodeError)

_UNAVAILABLE_ISSUERS_SQL = """
    select distinct p.normalized_payload->>'issuer_id'
    from raw.capture_obligations ob
    join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
    join staging.capture_observation_payloads p on p.observation_id = link.observation_id
    where ob.run_id = %s
      and p.normalized_payload->>%s = 'true'
    order by 1
"""


@dataclass(frozen=True)
class StatementFiling:
    form: str
    filed: date
    period_end: date

    @property
    def annual(self) -> bool:
        return self.form in ANNUAL_STATEMENT_FORMS


def statement_filings(submissions: Mapping[str, Any]) -> tuple[StatementFiling, ...]:
    recent = submissions.get("filings", {}).get("recent", {})
    columns: Sequence[Sequence[Any]] = [recent.get(key) or () for key in ("form", "filingDate", "reportDate")]
    filings: list[StatementFiling] = []
    for form, filed, period_end in zip(*columns, strict=False):
        if form not in STATEMENT_FORMS or not filed or not period_end:
            continue
        try:
            filings.append(StatementFiling(form, date.fromisoformat(filed), date.fromisoformat(period_end)))
        except (TypeError, ValueError):
            continue
    return tuple(filings)


def operating_branch_for_sic(sic: str | None) -> OperatingBranch:
    if sic is None or not sic.strip().isdigit():
        return OperatingBranch.NON_FINANCIAL
    code = int(sic.strip())
    if code in _DEPOSITORY_INSTITUTION_SIC:
        return OperatingBranch.FINANCIAL
    if code in _INSURANCE_SIC:
        return OperatingBranch.INSURANCE
    return OperatingBranch.NON_FINANCIAL


@dataclass(frozen=True)
class IssuerClassification:
    operating_branch: OperatingBranch
    statement_filings: tuple[StatementFiling, ...] = ()
    sic: str | None = None
    sic_description: str | None = None
    category: str | None = None
    state_of_incorporation: str | None = None
    fiscal_year_end: str | None = None
    primary_exchange: str | None = None
    entity_type: str | None = None


@dataclass(frozen=True)
class RegistryReading:
    classifications: dict[int, IssuerClassification]
    unavailable: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Read:
    submissions: dict[str, Any] | None
    reason: str = ""
    outage: bool = False


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _failure(error: Exception) -> tuple[bool, bool]:
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return (status == 429 or status >= 500), status != 404
    return isinstance(error, _TRANSIENT), True


def _read_submissions(cik: int, http: httpx.Client, *, attempts: int) -> _Read:
    try:
        return _Read(
            sec.fetch_company_submissions(
                cik,
                http,
                max_attempts=attempts,
                retry_statuses=(429, 500, 502, 503, 504),
                backoff_base=REGISTRY_BACKOFF_SECONDS,
                sleep=_pause,
            )
        )
    except (httpx.HTTPError, ValueError, CapacityExceeded) as error:
        _retry, outage = _failure(error)
        return _Read(None, f"{type(error).__name__}: {error}"[:300], outage)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _primary_exchange(submissions: Mapping[str, Any]) -> str | None:
    raw = submissions.get("exchanges")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if (clean := _clean_text(item)) is not None:
                return clean
    return _clean_text(submissions.get("exchange"))


def resolve_issuer_classifications(ciks: Mapping[str, int]) -> RegistryReading:
    classifications: dict[int, IssuerClassification] = {}
    unavailable: dict[int, str] = {}
    failed_in_a_row = 0
    with sec.client() as http:
        http.timeout = httpx.Timeout(REGISTRY_READ_TIMEOUT_SECONDS)
        for cik in sorted(set(ciks.values())):
            read = _read_submissions(
                cik, http, attempts=1 if failed_in_a_row >= REGISTRY_OUTAGE_AFTER else REGISTRY_ATTEMPTS
            )
            if read.submissions is None:
                logger.warning("registry unavailable for CIK %010d: %s", cik, read.reason)
                unavailable[cik] = read.reason
                failed_in_a_row += read.outage
                if read.outage and failed_in_a_row == REGISTRY_OUTAGE_AFTER:
                    logger.warning(
                        "registry down: %d issuers in a row failed; one attempt for each remaining issuer",
                        failed_in_a_row,
                    )
                continue
            failed_in_a_row = 0
            sub = read.submissions
            classifications[cik] = IssuerClassification(
                operating_branch=operating_branch_for_sic(sub.get("sic")),
                statement_filings=statement_filings(sub),
                sic=_clean_text(sub.get("sic")),
                sic_description=_clean_text(sub.get("sicDescription")),
                category=_clean_text(sub.get("category")),
                state_of_incorporation=_clean_text(sub.get("stateOfIncorporation")),
                fiscal_year_end=_clean_text(sub.get("fiscalYearEnd")),
                primary_exchange=_primary_exchange(sub),
                entity_type=_clean_text(sub.get("entityType")),
            )
    return RegistryReading(classifications=classifications, unavailable=unavailable)


def _fetch_ids_by_cik(connection: Any, ciks: Sequence[int]) -> dict[int, int]:
    """Return latest `raw.fetches.id` per CIK for SEC submissions payloads."""
    if not ciks:
        return {}
    patterns = [f"submissions:issuer:cik:{cik:010d}:%" for cik in sorted(set(ciks))]
    cursor = connection.execute(
        """
        select split_part(source_record_id, ':', 4) as cik_text, id
        from raw.fetches
        where source = 'sec'
          and source_record_id like any(%s::text[])
        order by fetched_at desc, id desc
        """,
        (patterns,),
    )
    rows = cursor.fetchall() if cursor is not None else ()
    out: dict[int, int] = {}
    for row in rows or ():
        if not row or len(row) < 2:
            continue
        cik_text, fetch_id = str(row[0] or "").strip(), row[1]
        if cik_text.isdigit() and fetch_id is not None:
            out.setdefault(int(cik_text), int(fetch_id))
    return out


def persist_issuer_profiles(
    connection: Any,
    *,
    classifications: Mapping[int, IssuerClassification],
    issuer_ids_by_cik: Mapping[int, Sequence[str]],
    knowable_at: datetime | None = None,
) -> int:
    """Upsert SEC submissions profile metadata into staging.issuer_profiles when present."""
    if connection is None or not classifications:
        return 0
    reg_row = connection.execute("select to_regclass('staging.issuer_profiles')").fetchone()
    if reg_row is None or reg_row[0] is None:
        return 0
    effective_ts = knowable_at or datetime.now(UTC)
    resolved_uuids: dict[int, set[str]] = {}
    legacy_ciks: list[int] = []
    for cik, raw_ids in issuer_ids_by_cik.items():
        if cik not in classifications:
            continue
        for raw_id in raw_ids:
            try:
                resolved_uuids.setdefault(cik, set()).add(str(uuid.UUID(str(raw_id))))
            except (ValueError, TypeError):
                legacy_ciks.append(cik)
    if legacy_ciks:
        padded = [f"{c:010d}" for c in sorted(set(legacy_ciks))]
        ei_rows = connection.execute(
            "select entity_id::text, cik from mart.entity_identity where kind = 'issuer' and cik = any(%s::text[])",
            (padded,),
        ).fetchall()
        for eid, cik_text in ei_rows or ():
            if cik_text and str(cik_text).isdigit():
                resolved_uuids.setdefault(int(str(cik_text)), set()).add(str(eid))
    fetch_ids = _fetch_ids_by_cik(connection, list(resolved_uuids))
    written = 0
    for cik in sorted(resolved_uuids):
        cls = classifications[cik]
        fetch_id = fetch_ids.get(cik)
        for iid in sorted(resolved_uuids[cik]):
            connection.execute(
                """
                insert into staging.issuer_profiles (
                    issuer_id, cik, sic, sic_description, category,
                    state_of_incorporation, fiscal_year_end, primary_exchange,
                    entity_type, source_fetch_id, knowable_at, recorded_at
                ) values (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())
                on conflict (issuer_id) do update set
                    cik = excluded.cik,
                    sic = coalesce(excluded.sic, staging.issuer_profiles.sic),
                    sic_description = coalesce(excluded.sic_description, staging.issuer_profiles.sic_description),
                    category = coalesce(excluded.category, staging.issuer_profiles.category),
                    state_of_incorporation = coalesce(excluded.state_of_incorporation, staging.issuer_profiles.state_of_incorporation),
                    fiscal_year_end = coalesce(excluded.fiscal_year_end, staging.issuer_profiles.fiscal_year_end),
                    primary_exchange = coalesce(excluded.primary_exchange, staging.issuer_profiles.primary_exchange),
                    entity_type = coalesce(excluded.entity_type, staging.issuer_profiles.entity_type),
                    source_fetch_id = coalesce(excluded.source_fetch_id, staging.issuer_profiles.source_fetch_id),
                    knowable_at = least(staging.issuer_profiles.knowable_at, excluded.knowable_at),
                    recorded_at = clock_timestamp()
                """,
                (
                    iid,
                    cik,
                    cls.sic,
                    cls.sic_description,
                    cls.category,
                    cls.state_of_incorporation,
                    cls.fiscal_year_end,
                    cls.primary_exchange,
                    cls.entity_type,
                    fetch_id,
                    effective_ts,
                ),
            )
            written += 1
    return written


def registry_unavailable_issuers(connection: Any, *, run_id: str) -> list[str]:
    rows = connection.execute(_UNAVAILABLE_ISSUERS_SQL, (run_id, UNAVAILABLE_PAYLOAD_KEY)).fetchall()
    return [str(issuer_id) for (issuer_id,) in rows if issuer_id is not None]
