"""Issuer registry metadata and operating branch classification."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx
from factors.production_topt import OperatingBranch

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
    for attempt in range(attempts):
        try:
            return _Read(sec.fetch_company_submissions(cik, http))
        except (httpx.HTTPError, ValueError, CapacityExceeded) as error:
            retry, outage = _failure(error)
            if not retry or attempt == attempts - 1:
                return _Read(None, f"{type(error).__name__}: {error}"[:300], outage)
            _pause(REGISTRY_BACKOFF_SECONDS * 2**attempt)
    raise AssertionError("unreachable")


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
            classifications[cik] = IssuerClassification(
                operating_branch=operating_branch_for_sic(read.submissions.get("sic")),
                statement_filings=statement_filings(read.submissions),
            )
    return RegistryReading(classifications=classifications, unavailable=unavailable)


def registry_unavailable_issuers(connection: Any, *, run_id: str) -> list[str]:
    rows = connection.execute(_UNAVAILABLE_ISSUERS_SQL, (run_id, UNAVAILABLE_PAYLOAD_KEY)).fetchall()
    return [str(issuer_id) for (issuer_id,) in rows if issuer_id is not None]
