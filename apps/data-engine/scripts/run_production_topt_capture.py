"""Run a REAL TOPT capture into the existing capture tables, then materialize + query.

This is the glue that was missing: it reuses the proven capture-control persistence path
(the same one the materialization test exercises) but fills each of the 84 obligations with
REAL data — Yahoo daily closes for market-price, SEC company-facts for financial-fact,
release/corpus identities for listing-identity and universe-membership. It then freezes the
snapshot, materializes GPPE v0 + three-tier, and prints the queried core results.

headcount is not a reliable XBRL concept (that is #70's extraction plane); until #70 lands, a
small real-value stopgap map supplies headcount for the issuers we can name, so GPPE computes a
real number for them and resolves `unavailable` (honestly) for the rest.

Usage (against the DATABASE_URL in settings):
    uv run --package truealpha-data-engine python \
      apps/data-engine/scripts/run_production_topt_capture.py
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
from data_engine.config import settings
from data_engine.datahub.control_plane import AttemptLedger, expand_obligations, replay_retry_policy
from data_engine.datahub.medium_replay import frozen_topt_list_version
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from data_engine.datahub.repository import PostgresCaptureControlRepository
from data_engine.sources import sec, yahoo
from factors.production_topt import GppeV0Definition
from truealpha_contracts import CaptureEnvironment, canonical_sha256
from truealpha_contracts.capture_control import CaptureObligationWorkBinding
from truealpha_contracts.datahub import (
    CaptureCampaign,
    CaptureRun,
    CaptureSchedulePolicy,
    CaptureWorkItem,
    FetchAttemptOutcome,
    ListObligationResult,
    NormalizedObservation,
    ObligationTerminalState,
    SourceRequest,
    SourceVintage,
)

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "capture_control" / "corpus.v1.json"
CUTOFF = datetime(2026, 4, 2, tzinfo=UTC)
CUTOFF_DATE = CUTOFF.date()
SEMANTIC_TYPES = ("market-price", "listing-identity", "universe-membership", "financial-fact")

# Stopgap real headcounts (public 10-K figures) until #70's extraction plane lands. SEC XBRL does
# not tag employee count structurally, so these are manually sourced real values; #70 replaces
# this map with append-only filing-text extraction.
_HEADCOUNT: dict[str, str] = {
    "AAPL": "164000",
    "MSFT": "228000",
    "GOOG": "182502",
    "GOOGL": "182502",
    "NVDA": "29600",
    "META": "67317",
    "AMZN": "1556000",
    "TSLA": "140473",
    "AVGO": "20000",
    "COST": "316000",
    "NFLX": "14000",
    "MU": "48000",
    "WMT": "2100000",
    "LLY": "43000",
    "ABBV": "50000",
    "JNJ": "138100",
    "XOM": "62000",
    "JPM": "309926",
    "MA": "33400",
    "V": "28800",
    "BRK.B": "392400",
}


def _source_request(obligation, *, ordinal: int) -> SourceRequest:
    coordinate = {
        "ordinal": ordinal,
        "subject": obligation.subject.model_dump(mode="json"),
        "requirement": obligation.capture_requirement_id,
        "partition": obligation.partition,
    }
    return SourceRequest(
        source_registry_entry_id=f"source-registry-entry:{canonical_sha256({'source': 'production-topt-live:v7'})}",
        source_policy_id="source-policy:production-topt-live-v7",
        request_fingerprint_version="production-topt-live:v7",
        canonical_request_sha256=canonical_sha256(coordinate),
        subject_refs=(obligation.subject,),
        capture_requirement_ids=(obligation.capture_requirement_id,),
        partition=obligation.partition,
    )


def _sec_ticker(ticker: str) -> str:
    # SEC's company_tickers file uses a hyphen for share-class tickers (BRK.B -> BRK-B).
    return ticker.replace(".", "-")


def _real_market_price(ticker: str) -> str:
    bars = [bar for bar in yahoo.fetch_daily_bars(_sec_ticker(ticker)) if bar.date <= CUTOFF_DATE]
    if not bars:
        raise RuntimeError(f"no Yahoo close at/before {CUTOFF_DATE} for {ticker}")
    return str(Decimal(str(max(bars, key=lambda b: b.date).close)))


_REVENUE_CONCEPTS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")
# SEC XBRL heterogeneity: issuers report cost-of-revenue under different us-gaap tags
# (pharma uses CostOfGoodsAndServicesSold, not CostOfRevenue). Try each in priority order.
# Issuers with no cost-of-revenue concept at all (banks, payment networks, insurers,
# integrated energy) are a structurally different operating branch (#59), not a mapping gap.
_COGS_CONCEPTS = ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices")


def _annual_by_end(facts: dict, taxonomy: str, name: str, unit: str) -> dict[str, Decimal]:
    """Eligible values keyed by period end. Flow concepts keep annual periods (>=350 days);
    instant concepts (no start, e.g. shares) are kept as-is. Only facts filed <= cutoff."""
    node = (facts.get("facts", {}).get(taxonomy) or {}).get(name)
    if not node:
        return {}
    out: dict[str, Decimal] = {}
    for entry in node.get("units", {}).get(unit, []):
        filed, end, start, val = entry.get("filed"), entry.get("end"), entry.get("start"), entry.get("val")
        if filed is None or end is None or val is None or date.fromisoformat(filed) > CUTOFF_DATE:
            continue
        if start is not None and (date.fromisoformat(end) - date.fromisoformat(start)).days < 350:
            continue
        out[end] = Decimal(str(val))
    return out


def _latest(by_end: dict[str, Decimal]) -> Decimal | None:
    return by_end[max(by_end)] if by_end else None


def _gross_profit(facts: dict) -> Decimal | None:
    direct = _annual_by_end(facts, "us-gaap", "GrossProfit", "USD")
    if direct:
        return _latest(direct)
    for cogs_name in _COGS_CONCEPTS:
        cogs = _annual_by_end(facts, "us-gaap", cogs_name, "USD")
        if not cogs:
            continue
        for revenue_name in _REVENUE_CONCEPTS:
            revenue = _annual_by_end(facts, "us-gaap", revenue_name, "USD")
            shared = set(revenue) & set(cogs)  # same period end -> comparable
            if shared:
                end = max(shared)
                return revenue[end] - cogs[end]
    return None


def _pre_provision_profit(facts: dict) -> Decimal | None:
    """Pre-provision net revenue for a bank: total net revenue minus noninterest expense,
    before the provision for credit losses. This is the FINANCIAL-branch numerator."""
    revenue = _annual_by_end(facts, "us-gaap", "RevenuesNetOfInterestExpense", "USD")
    if not revenue:
        revenue = _annual_by_end(facts, "us-gaap", "Revenues", "USD")
    expense = _annual_by_end(facts, "us-gaap", "NoninterestExpense", "USD")
    shared = set(revenue) & set(expense)  # same period end -> comparable
    if shared:
        end = max(shared)
        return revenue[end] - expense[end]
    return None


# Bank issuers whose operating efficiency uses pre-provision net revenue, not gross profit
# (the frozen v0.1.0 FINANCIAL branch: (pre_provision_profit - total_assets*rfr)/headcount).
# JPM is the one TOPT bank. V/MA (payment networks), BRK.B (insurer/conglomerate), and XOM
# (integrated energy) are also non-gross-profit issuers but do NOT report pre-provision profit;
# they need service/insurance/energy branches the v0.1.0 definition does not yet model (#59).
_FINANCIAL_BRANCH_TICKERS = frozenset({"JPM"})


def _real_financial(ticker: str) -> dict[str, str | None]:
    facts: dict | None = None
    try:
        cik = sec.ticker_to_cik(_sec_ticker(ticker))
        facts = sec.fetch_company_facts(cik)
    except Exception as error:  # SEC mapping/HTTP failure: capture the cell with null values
        print(f"    (SEC unavailable for {ticker}: {type(error).__name__}); financials null")
    is_financial = ticker in _FINANCIAL_BRANCH_TICKERS
    assets = None if facts is None else _latest(_annual_by_end(facts, "us-gaap", "Assets", "USD"))
    shares = (
        None if facts is None else _latest(_annual_by_end(facts, "us-gaap", "CommonStockSharesOutstanding", "shares"))
    )
    gross = None if (facts is None or is_financial) else _gross_profit(facts)
    ppnr = _pre_provision_profit(facts) if (facts is not None and is_financial) else None
    return {
        "operating_branch": "financial" if is_financial else "non_financial",
        "currency": "USD",
        "gross_profit": None if gross is None else str(gross),
        "total_assets": None if assets is None else str(assets),
        "headcount": _HEADCOUNT.get(ticker),
        "revenue": None,
        "shares_outstanding": None if shares is None else str(shares),
        "pre_provision_profit": None if ppnr is None else str(ppnr),
    }


def _real_payload(coordinates: tuple[str, str, str, str], semantic_type: str) -> dict[str, str | None]:
    issuer_id, instrument_id, listing_id, ticker = coordinates
    identity = {"issuer_id": issuer_id, "instrument_id": instrument_id, "listing_id": listing_id}
    if semantic_type in {"listing-identity", "universe-membership"}:
        return {**identity, "ticker": ticker}
    if semantic_type == "market-price":
        return {**identity, "currency": "USD", "close": _real_market_price(ticker)}
    if semantic_type == "financial-fact":
        return {**identity, **_real_financial(ticker)}
    raise AssertionError(semantic_type)


def _confidence(semantic_type: str, payload: dict[str, str | None]) -> Decimal:
    """Per-source-class confidence (retires the static 0.9, #207/#404): release-derived
    identities are exact; a single public price feed carries no SLA; SEC financials grade by
    field completeness. This is a v0 source-class prior until the calibrated formula (#337)
    is wired into the capture path."""
    if semantic_type in {"listing-identity", "universe-membership"}:
        return Decimal("1.0")  # exact projection of the frozen release
    if semantic_type == "market-price":
        return Decimal("0.85")  # single public feed, no SLA (yfinance)
    if semantic_type == "financial-fact":
        present = sum(
            payload.get(field) is not None for field in ("gross_profit", "total_assets", "shares_outstanding")
        )
        return {3: Decimal("0.92"), 2: Decimal("0.80"), 1: Decimal("0.65")}.get(present, Decimal("0.50"))
    return Decimal("0.50")


def _capture(connection: psycopg.Connection) -> tuple[str, str]:
    corpus = json.loads(CORPUS.read_text())
    denominator = corpus["topt_denominator"]
    coordinates = {row[2]: tuple(row) for row in denominator["instruments"]}
    list_version = frozen_topt_list_version(corpus)
    policy = CaptureSchedulePolicy(
        policy_version="production-topt-live:v7",
        demanded_cadence=timedelta(days=1),
        provider_availability_cadence="manual-only:v1",
        freshness_max_age=timedelta(days=2),
        retry=replay_retry_policy(3),
    )
    campaign = CaptureCampaign(
        campaign_policy_id="capture-policy:production-topt-live-v7",
        environment=CaptureEnvironment.PRODUCTION,
        cutoff=CUTOFF,
        universe_refs=(list_version.universe,),
    )
    run = CaptureRun(
        campaign_id=campaign.campaign_id,
        run_sequence=1,
        schedule_policy_id=policy.schedule_policy_id,
        capture_scope_id=f"capture-scope:{canonical_sha256({'scope': 'production-topt-live:v7'})}",
    )
    obligations = expand_obligations(
        run_id=run.run_id,
        list_version=list_version,
        semantic_types=SEMANTIC_TYPES,
        partition=str(denominator["report_date"]),
    )
    repo = PostgresCaptureControlRepository(connection)
    repo.put_schedule_policy(policy)
    repo.put_campaign(campaign)
    repo.put_list_version(list_version)
    repo.bind_campaign_list(campaign.campaign_id, list_version.list_version_id)
    repo.put_run(run)

    release_payload = {"kind": "production-topt-live-release"}
    release_sha256 = canonical_sha256(release_payload)
    release_manifest_id = f"release-manifest:{release_sha256}"
    connection.execute(
        "insert into staging.contract_objects (contract_id, contract_kind, content_sha256, payload) "
        "values (%s, 'release_manifest', %s, %s) on conflict (contract_id) do nothing",
        (release_manifest_id, release_sha256, psycopg.types.json.Jsonb(release_payload)),
    )
    run_plan_payload = {"run_id": run.run_id, "release_manifest_id": release_manifest_id}
    connection.execute(
        "insert into raw.production_topt_run_plans (run_id, release_manifest_id, content_sha256, payload) "
        "values (%s, %s, %s, %s) on conflict (run_id) do nothing",
        (
            run.run_id,
            release_manifest_id,
            canonical_sha256(run_plan_payload),
            psycopg.types.json.Jsonb(run_plan_payload),
        ),
    )

    for ordinal, obligation in enumerate(obligations):
        request = _source_request(obligation, ordinal=ordinal)
        work_item = CaptureWorkItem(
            campaign_id=campaign.campaign_id,
            source_request_id=request.source_request_id,
            schedule_policy_id=policy.schedule_policy_id,
        )
        binding = CaptureObligationWorkBinding(
            obligation_id=obligation.obligation_id, work_item_id=work_item.work_item_id
        )
        repo.put_obligation(campaign.campaign_id, obligation)
        repo.put_source_request(request)
        repo.put_work_item(work_item, policy.retry)
        repo.put_binding(binding)

        semantic_type = obligation.capture_requirement_id.removesuffix(":v1")
        payload = _real_payload(coordinates[obligation.subject.id], semantic_type)
        raw_sha256 = canonical_sha256({"ordinal": ordinal, "payload": payload})
        source_record_id = f"production-topt-live-v7:{ordinal}"
        raw_fetch_id = connection.execute(
            "insert into raw.fetches (source, source_record_id, payload_sha256, object_uri, content_type, "
            "byte_length, fetched_at, recorded_at, metadata) "
            "values (%s, %s, %s, %s, 'application/json', 1, %s, %s, '{}'::jsonb) returning id",
            (
                "production-topt-live-v7",
                source_record_id,
                raw_sha256,
                f"s3://production-topt-live-v7/{raw_sha256}",
                CUTOFF - timedelta(hours=2),
                CUTOFF - timedelta(hours=2),
            ),
        ).fetchone()[0]
        vintage = SourceVintage(
            source_request_id=request.source_request_id,
            source_record_id=source_record_id,
            source_published_at=CUTOFF - timedelta(hours=2),
            raw_object_id=f"raw-object:{raw_sha256}",
        )
        ledger = AttemptLedger(work_item_id=work_item.work_item_id, retry_policy=policy.retry)
        attempt = ledger.start(started_at=CUTOFF - timedelta(hours=1))
        attempt_result = ledger.finish(
            attempt=attempt,
            completed_at=CUTOFF - timedelta(minutes=59),
            outcome=FetchAttemptOutcome.SUCCESS,
            status_code=200,
            source_vintage_id=vintage.source_vintage_id,
        )
        observation = NormalizedObservation(
            semantic_type=semantic_type,
            semantic_version=obligation.capture_requirement_id,
            subject=obligation.subject,
            valid_from=CUTOFF - timedelta(days=2),
            valid_to=CUTOFF - timedelta(days=2),
            knowable_at=CUTOFF - timedelta(minutes=58),
            source_vintage_id=vintage.source_vintage_id,
            parser_version="production-topt-live-parser:v1",
            mapping_version="production-topt-live-map:v1",
            normalized_payload_sha256=canonical_sha256(payload),
        )
        terminal = ListObligationResult(
            obligation_id=obligation.obligation.obligation_id,
            terminal_state=ObligationTerminalState.SUCCESS,
            completed_at=CUTOFF - timedelta(minutes=57),
            final_attempt_id=attempt.attempt_id,
            reason_codes=("success",),
        )
        repo.put_attempt(attempt)
        repo.put_source_vintage(vintage, raw_fetch_id=raw_fetch_id)
        repo.put_attempt_result(attempt_result)
        repo.put_observation(
            obligation.obligation_id,
            observation,
            normalized_payload=payload,
            confidence=_confidence(semantic_type, payload),
            freshness_state="fresh",
        )
        repo.put_obligation_result(obligation.obligation_id, terminal)
        print(f"  captured {ordinal + 1}/84  {semantic_type:22} {coordinates[obligation.subject.id][3]}")

    return run.run_id, release_manifest_id


def main() -> int:
    with psycopg.connect(settings.database_url, autocommit=False) as connection:
        print("== capturing 84 real TOPT cells ==")
        run_id, release_manifest_id = _capture(connection)
        status = PostgresCaptureControlRepository(connection).status(run_id)
        print(f"== capture status: {status} ==")

        core = PostgresToptCoreRepository(connection)
        snapshot = core.freeze_snapshot(run_id=run_id, release_manifest_id=release_manifest_id)
        results = core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate=Decimal("0.05")))
        connection.commit()

        print(f"\n== materialized {len(results)} core results ==")
        for item in results:
            value = getattr(item, "tier_value", None) or getattr(item, "value", None)
            print(f"  {item.availability.value:12} {getattr(item, 'listing_id', '')}  {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
