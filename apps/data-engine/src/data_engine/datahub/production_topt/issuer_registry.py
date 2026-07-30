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

from collections.abc import Mapping
from dataclasses import dataclass

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


class IssuerRegistryUnavailableError(RuntimeError):
    """The registry could not classify an issuer, so no run may assume a branch."""


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


def resolve_issuer_classifications(ciks: Mapping[str, int]) -> dict[int, IssuerClassification]:
    """Classify every distinct CIK in the run's universe, once, from one submissions read.

    A registry that cannot be reached raises: a run that cannot say whether an issuer is
    a bank must not silently classify it as anything.
    """
    classifications: dict[int, IssuerClassification] = {}
    with sec.client() as http:
        for cik in sorted(set(ciks.values())):
            try:
                submissions = sec.fetch_company_submissions(cik, http)
            except httpx.HTTPError as error:
                raise IssuerRegistryUnavailableError(
                    f"could not resolve the operating branch for CIK {cik:010d}: {error}"
                ) from error
            sic = submissions.get("sic")
            classifications[cik] = IssuerClassification(
                operating_branch=operating_branch_for_sic(sic),
                revenue_proxy_allowed=revenue_proxy_allowed_for_sic(sic),
            )
    return classifications


def resolve_operating_branches(ciks: Mapping[str, int]) -> dict[int, OperatingBranch]:
    """Operating branch only, for callers that do not need the full classification."""
    return {cik: item.operating_branch for cik, item in resolve_issuer_classifications(ciks).items()}
