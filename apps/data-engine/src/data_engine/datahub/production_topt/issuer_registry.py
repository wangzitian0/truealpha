"""Issuer registry metadata for TOPT capture (#59 / #171 A2a).

The operating branch decides which operating numerator a financial-fact record asserts: a
depository institution files no gross profit, so its numerator is the pre-provision net
revenue proxy. That classification is a property of the *issuer*, so it is resolved from
issuer registry metadata — SEC EDGAR's SIC classification — never from a ticker allowlist
baked into capture code.

SIC major group 60 is "Depository Institutions" (commercial banks, savings institutions,
credit unions). Insurers (63xx), brokers (62xx) and payment processors (73xx) are *not*
depository institutions: their operating economics are different branches again (#59), and
until those branches exist they stay non-financial and surface their gap honestly rather
than being scored through a proxy that does not describe them.
"""

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

_DEPOSITORY_INSTITUTION_SIC = range(6000, 6100)
_INSURANCE_SIC = range(6300, 6400)

# The industries whose cost of revenue really is ~zero, and so may substitute revenue for
# gross profit when the issuer tags neither concept (#496 owner decision, 2026-07-28).
#
# The decision named payment networks — "payment networks carry ~zero COGS; the bias is
# small and its direction known". It was implemented as "no GrossProfit and no COGS-family
# concept at all", which also captures issuers whose COGS is enormous but simply untagged:
# XOM (SIC 2911, Petroleum Refining) took the proxy and published gross profit equal to its
# $332B revenue, or $5.36M per employee — ranking Exxon Mobil above NVIDIA as the most
# labour-efficient issuer in the universe, selected at half the target weight (#533).
#
# So eligibility is a property of the industry, not of which tags happen to be missing.
# Resolved from the EDGAR SIC the same way the operating branch is — registry metadata,
# never a ticker allowlist. V and MA are 7389; widening this set is a one-line versioned
# change if the owner intends another industry to qualify.
_NO_COGS_SERVICES_SIC = frozenset({7389})


# The EDGAR forms that carry an issuer's periodic financial statements: audited annual
# reports and reviewed quarterly ones, with their amendments and transition-period variants.
# A figure asserted in any other form (a proxy statement's pay-versus-performance table, an
# earnings-release 8-K, a 6-K) is not the statement: DEF 14A carried FANG's FY2025 net income
# at $1,547M against the 10-K's $1,664M (-7.0%), and it won on filing date alone.
ANNUAL_STATEMENT_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"})
INTERIM_STATEMENT_FORMS = frozenset({"10-Q", "10-Q/A", "10-QT", "10-QT/A"})
STATEMENT_FORMS = ANNUAL_STATEMENT_FORMS | INTERIM_STATEMENT_FORMS


@dataclass(frozen=True)
class StatementFiling:
    """One periodic-statement filing in the issuer's EDGAR index: what period it reports
    and when it became public. The index lists a filing the day it is accepted, while the
    company-facts API can lag it by a quarter (V, PYPL and NXPI in 2026-09), so this is the
    newest period the pipeline can prove was filed."""

    form: str
    filed: date
    period_end: date

    @property
    def annual(self) -> bool:
        return self.form in ANNUAL_STATEMENT_FORMS


def statement_filings(submissions: Mapping[str, Any]) -> tuple[StatementFiling, ...]:
    """Every periodic-statement filing the submissions document's recent index lists."""
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
    """Map an EDGAR SIC code to the operating branch its economics belong to."""
    if sic is None or not sic.strip().isdigit():
        return OperatingBranch.NON_FINANCIAL
    code = int(sic.strip())
    if code in _DEPOSITORY_INSTITUTION_SIC:
        return OperatingBranch.FINANCIAL
    if code in _INSURANCE_SIC:
        return OperatingBranch.INSURANCE
    return OperatingBranch.NON_FINANCIAL


def revenue_proxy_allowed_for_sic(sic: str | None) -> bool:
    """Whether this industry may substitute revenue for an untagged gross profit (#533).

    Unclassifiable resolves to False: an issuer we cannot place in an industry must not
    receive a substitution that only holds for one.
    """
    if sic is None or not sic.strip().isdigit():
        return False
    return int(sic.strip()) in _NO_COGS_SERVICES_SIC


@dataclass(frozen=True)
class IssuerClassification:
    """What the registry asserts about one issuer's economics."""

    operating_branch: OperatingBranch
    revenue_proxy_allowed: bool
    # The periodic-statement filings the EDGAR index lists, read from the same submissions
    # document, so a capture can tell when its company-facts figures trail a filed period.
    statement_filings: tuple[StatementFiling, ...] = ()


#: Attempts to read one issuer's submissions (#1323). Three attempts wait 1 s, then 2 s.
REGISTRY_ATTEMPTS = 3
REGISTRY_BACKOFF_SECONDS = 1.0
#: Issuers in a row that spent every attempt on a transient failure. At this count the registry
#: is down and the remaining issuers get no attempt. Each attempt can wait for the client timeout
#: (30 s), so 20 issuers would hold the tick transaction for 30 minutes.
REGISTRY_OUTAGE_AFTER = 3
#: The payload key of a financial-fact observation whose issuer the registry could not classify.
UNAVAILABLE_PAYLOAD_KEY = "registry_unavailable"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegistryReading:
    """What one registry pass found: the classified issuers, and the ones it could not classify."""

    classifications: dict[int, IssuerClassification]
    #: CIK -> why the registry gave no classification. An issuer here is excluded, never assumed.
    unavailable: dict[int, str] = field(default_factory=dict)


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _transient(error: httpx.HTTPError | json.JSONDecodeError) -> bool:
    """True for a failure the next attempt can clear: a timeout, a reset, a 429 or a 5xx."""
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code == 429 or error.response.status_code >= 500
    return isinstance(error, httpx.TransportError | json.JSONDecodeError)


def _read_submissions(cik: int, http: httpx.Client) -> tuple[dict[str, Any] | None, str | None, bool]:
    """(submissions, None, False) on success; (None, reason, transient) when every attempt failed.

    Only a transient failure is retried, with a doubling wait between attempts.
    """
    for attempt in range(REGISTRY_ATTEMPTS):
        try:
            return sec.fetch_company_submissions(cik, http), None, False
        except (httpx.HTTPError, json.JSONDecodeError) as error:
            transient = _transient(error)
            reason = f"{type(error).__name__}: {error}"[:300]
            if not transient or attempt == REGISTRY_ATTEMPTS - 1:
                return None, reason, transient
            _pause(REGISTRY_BACKOFF_SECONDS * 2**attempt)
    raise AssertionError("unreachable: the last attempt returns")


def resolve_issuer_classifications(ciks: Mapping[str, int]) -> RegistryReading:
    """Classify every distinct CIK in the run's universe, once, from one submissions read.

    A read that fails with a transient error is tried again, up to `REGISTRY_ATTEMPTS`. An issuer
    whose read still fails is named in `RegistryReading.unavailable`. The caller excludes that
    issuer: a run that cannot say whether an issuer is a bank must not classify it as anything.
    One issuer never fails the run (#1323).
    """
    classifications: dict[int, IssuerClassification] = {}
    unavailable: dict[int, str] = {}
    exhausted_in_a_row = 0
    with sec.client() as http:
        for cik in sorted(set(ciks.values())):
            if exhausted_in_a_row >= REGISTRY_OUTAGE_AFTER:
                unavailable[cik] = (
                    f"not read: {exhausted_in_a_row} issuers in a row spent {REGISTRY_ATTEMPTS} attempts on failures"
                )
                continue
            submissions, reason, transient = _read_submissions(cik, http)
            if submissions is None:
                logger.warning("registry unavailable for CIK %010d: %s", cik, reason)
                unavailable[cik] = reason or "no reason"
                exhausted_in_a_row = exhausted_in_a_row + 1 if transient else 0
                continue
            exhausted_in_a_row = 0
            sic = submissions.get("sic")
            classifications[cik] = IssuerClassification(
                operating_branch=operating_branch_for_sic(sic),
                revenue_proxy_allowed=revenue_proxy_allowed_for_sic(sic),
                statement_filings=statement_filings(submissions),
            )
    return RegistryReading(classifications=classifications, unavailable=unavailable)


_UNAVAILABLE_ISSUERS_SQL = """
    select distinct p.normalized_payload->>'issuer_id'
    from raw.capture_obligations ob
    join staging.capture_observation_obligations link on link.capture_obligation_id = ob.obligation_id
    join staging.capture_observation_payloads p on p.observation_id = link.observation_id
    where ob.run_id = %s
      and p.normalized_payload->>%s = 'true'
    order by 1
"""


def registry_unavailable_issuers(connection: Any, *, run_id: str) -> list[str]:
    """The issuer ids of the run whose financial-fact observation the registry could not classify."""
    rows = connection.execute(_UNAVAILABLE_ISSUERS_SQL, (run_id, UNAVAILABLE_PAYLOAD_KEY)).fetchall()
    return [str(issuer_id) for (issuer_id,) in rows if issuer_id is not None]
