"""Test support: the deployed SEC financial-fact route over stored company-facts bytes (#1379, #1457).

`build_route` is the route that a capture runs. This helper replaces only its two SEC reads and the
issuer classification. The route makes no network call.

With no `connection`, the route needs no database. The ruleset is then the shipped default, as in
production with no governed head. With a `connection`, `resolve_ruleset` reads the governed head first,
as a capture does, and the head can differ from the default.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from data_engine.config import settings
from data_engine.datahub.production_topt import issuer_registry
from data_engine.datahub.production_topt.executor import FetchSuccess
from data_engine.datahub.production_topt.sec_financial_adapter import build_route
from data_engine.datahub.production_topt.source_registrations import RouteCell, RouteContext
from data_engine.sources import sec
from factors.production_topt import OperatingBranch
from truealpha_contracts.datahub import CaptureWorkItem


def served_financial_record(
    monkeypatch: Any, body: bytes, *, cik: int, ticker: str, cutoff_date: date, connection: Any = None
) -> FetchSuccess:
    """The financial-fact record that the route stores for one issuer at one cutoff date.

    With no `connection` the ruleset is the shipped default. With one, `resolve_ruleset` reads the
    governed head first, as the capture does."""

    def company_facts(requested_cik: int) -> tuple[bytes, dict[str, Any]]:
        assert requested_cik == cik
        return body, json.loads(body)

    monkeypatch.setattr(sec, "fetch_company_facts_response", company_facts)
    monkeypatch.setattr(
        issuer_registry,
        "resolve_issuer_classifications",
        lambda ciks: {
            requested: issuer_registry.IssuerClassification(
                operating_branch=OperatingBranch.NON_FINANCIAL, revenue_proxy_allowed=False
            )
            for requested in ciks.values()
        },
    )
    monkeypatch.setattr(settings, "moomoo_financials_origin_enabled", False)
    issuer, instrument, listing = f"issuer:cik:{cik}", f"instrument:{ticker.lower()}", f"listing:xnas:{ticker.lower()}"
    work_item = CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + "c" * 64,
        schedule_policy_id="schedule-policy:" + "2" * 64,
    )
    context = RouteContext(
        cutoff=datetime.combine(cutoff_date, datetime.min.time(), tzinfo=UTC) + timedelta(hours=23, minutes=20),
        cutoff_date=cutoff_date,
        price_cutoff_date=cutoff_date,
        partition_start=datetime.combine(cutoff_date - timedelta(days=1), datetime.min.time(), tzinfo=UTC),
        universe_published_at=None,
        coordinates={listing: (issuer, instrument, listing, ticker)},
        connection=connection,
    )
    cell = RouteCell(work_item.work_item_id, "financial-fact", issuer, instrument, listing, ticker)
    outcome = build_route(context, [cell]).fetch(work_item)
    assert isinstance(outcome, FetchSuccess)
    assert outcome.record is not None
    return outcome
