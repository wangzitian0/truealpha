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

import httpx
from factors.production_topt import OperatingBranch

from data_engine.sources import sec

_DEPOSITORY_INSTITUTION_SIC = range(6000, 6100)
_INSURANCE_SIC = range(6300, 6400)


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


def resolve_operating_branches(ciks: Mapping[str, int]) -> dict[int, OperatingBranch]:
    """Classify every distinct CIK in the run's universe, once.

    A registry that cannot be reached raises: a run that cannot say whether an issuer is
    a bank must not silently classify it as anything.
    """
    branches: dict[int, OperatingBranch] = {}
    with sec.client() as http:
        for cik in sorted(set(ciks.values())):
            try:
                submissions = sec.fetch_company_submissions(cik, http)
            except httpx.HTTPError as error:
                raise IssuerRegistryUnavailableError(
                    f"could not resolve the operating branch for CIK {cik:010d}: {error}"
                ) from error
            branches[cik] = operating_branch_for_sic(submissions.get("sic"))
    return branches
