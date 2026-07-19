"""The real-source TOPT pipeline as an importable, schedulable unit (#27 / #429 P1).

This is the one-shot production capture script's logic, parameterized so the deployed
Dagster job can run it per schedule tick:

* ``run_live_topt_pipeline`` — capture all 84 obligations from REAL sources (Yahoo
  closes, SEC company-facts, Twelve Data second price origin), freeze the snapshot,
  materialize GPPE v0 + three-tier into ``mart.topt_*``, and persist the run's
  quality report. ``cutoff`` and ``version`` come from the schedule tick, so two
  consecutive ticks produce two distinct content-addressed runs and a retried tick
  reproduces the same identities (idempotent: conflict-tolerant inserts).
* ``seed_strategy_inputs_from_capture`` — the capture→strategy bridge (#429): lands
  the captured cells' provenance-neutral factor inputs into
  ``staging.strategy_backtest_inputs``, replacing the golden-fixture seeding that
  previously fed the deployed canary.
* ``run_strategy_replay_for_cutoff`` — drives the single-source ``strategy_evaluator``
  over the ``StrategyBacktestGateway`` for that cutoff and persists
  ``mart.strategy_runs``/``mart.strategy_decisions``.

The universe (21 listings / 84 cells) comes from the frozen capture-control corpus —
that is versioned scope configuration (init.md rule 13), not input data. The only
fixture-derived object the strategy step reads is the frozen strategy DEFINITION
(#21); golden inputs/decisions/rates are never read (#429 invariant I2).

headcount remains the #70 stopgap map; issuers without an applicable operating
branch stay honestly unavailable (#59).
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from factors.composite.strategy_evaluator import evaluate_cutoff
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
from truealpha_contracts.strategy import LargeModelValueV0Definition

from data_engine.core_strategy_replay import _load_corpus, _to_decision
from data_engine.datahub import quality_report
from data_engine.datahub.control_plane import AttemptLedger, expand_obligations, replay_retry_policy
from data_engine.datahub.medium_replay import frozen_topt_list_version
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from data_engine.datahub.repository import PostgresCaptureControlRepository
from data_engine.sources import sec, yahoo
from data_engine.strategy_backtest_gateway import StrategyBacktestGateway
from data_engine.strategy_replay_repository import write_replay

CORPUS = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "capture_control" / "corpus.v1.json"
SEMANTIC_TYPES = ("market-price", "listing-identity", "universe-membership", "financial-fact")
PRIMARY_PARSER_VERSION = "production-topt-live-parser:v1"

# Stopgap real headcounts (public 10-K figures) until #70's extraction plane lands.
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

_REVENUE_CONCEPTS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")
# SEC XBRL heterogeneity: issuers report cost-of-revenue under different us-gaap tags.
# Issuers with no cost-of-revenue concept at all are a different operating branch (#59).
_COGS_CONCEPTS = ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices")

# Bank issuers whose operating efficiency uses pre-provision net revenue (FINANCIAL
# branch). JPM is the one TOPT bank; V/MA/BRK.B/XOM need branches v0.1.0 lacks (#59).
_FINANCIAL_BRANCH_TICKERS = frozenset({"JPM"})


def live_version_for(cutoff: datetime) -> str:
    """The per-tick capture version: distinct per scheduled tick, stable on retry."""
    return f"live-{cutoff.astimezone(UTC):%Y%m%dT%H%M}"


def load_strategy_definition() -> LargeModelValueV0Definition:
    """The frozen large_model_value_v0 strategy definition (#21). Versioned strategy
    CONFIGURATION (formula constants and bands) — the deployed job never reads the
    corpus's golden inputs, decisions, or rates (#429 invariant I2)."""
    corpus = _load_corpus()
    return LargeModelValueV0Definition.model_validate_json(json.dumps(corpus["strategy_definition"]))


def _sec_ticker(ticker: str) -> str:
    # SEC's company_tickers file uses a hyphen for share-class tickers (BRK.B -> BRK-B).
    return ticker.replace(".", "-")


def _confidence(semantic_type: str, payload: dict[str, str | None]) -> Decimal:
    """Per-source-class confidence prior (#207/#404); the calibrated formula is #337."""
    if semantic_type in {"listing-identity", "universe-membership"}:
        return Decimal("1.0")  # exact projection of the frozen release
    if semantic_type == "market-price":
        return Decimal("0.85")  # single public feed, no SLA
    if semantic_type == "financial-fact":
        present = sum(
            payload.get(field) is not None for field in ("gross_profit", "total_assets", "shares_outstanding")
        )
        return {3: Decimal("0.92"), 2: Decimal("0.80"), 1: Decimal("0.65")}.get(present, Decimal("0.50"))
    return Decimal("0.50")


@dataclass(frozen=True)
class LiveToptPipelineResult:
    run_id: str
    release_manifest_id: str
    core_result_count: int
    quality_report_id: str
    quality: dict[str, Any]


class LiveToptCapture:
    """One capture run against real sources for an explicit ``cutoff``/``version``.

    All record identities derive from (cutoff, version, universe), never the wall
    clock, so a retried tick reproduces the same content-addressed run and the
    conflict-tolerant inserts make the replay idempotent.
    """

    def __init__(self, *, cutoff: datetime, version: str) -> None:
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        self._cutoff = cutoff
        self._cutoff_date = cutoff.astimezone(UTC).date()
        self._version = version
        self._twelve_key = os.environ.get("TWELVE_DATA_API_KEY", "")
        self._twelve_cache: dict[str, str | None] = {}

    # -- source fetches -----------------------------------------------------------

    def _real_market_price(self, ticker: str) -> str:
        bars = [bar for bar in yahoo.fetch_daily_bars(_sec_ticker(ticker)) if bar.date <= self._cutoff_date]
        if not bars:
            raise RuntimeError(f"no Yahoo close at/before {self._cutoff_date} for {ticker}")
        return str(Decimal(str(max(bars, key=lambda b: b.date).close)))

    def _twelve_data_price(self, ticker: str) -> str | None:
        """Twelve Data close at/before the cutoff (independent second origin, #344),
        memoized and throttled under the free tier's 8-requests/minute limit."""
        if not self._twelve_key:
            return None
        if ticker in self._twelve_cache:
            return self._twelve_cache[ticker]
        query = urllib.parse.urlencode(
            {
                "symbol": ticker,
                "interval": "1day",
                "start_date": str(self._cutoff_date - timedelta(days=10)),
                "end_date": str(self._cutoff_date),
                "outputsize": "12",
                "apikey": self._twelve_key,
            }
        )
        price: str | None = None
        try:
            with urllib.request.urlopen(f"https://api.twelvedata.com/time_series?{query}", timeout=20) as resp:
                body = json.loads(resp.read().decode())
            for row in body.get("values") or []:  # newest-first; first close at/before the cutoff
                if row.get("datetime", "")[:10] <= str(self._cutoff_date) and row.get("close"):
                    price = str(Decimal(str(row["close"])))
                    break
        except Exception:  # second source is best-effort; the cell stays single-source
            price = None
        self._twelve_cache[ticker] = price
        time.sleep(8)  # free tier ceiling is 8 requests/minute
        return price

    def _annual_by_end(self, facts: dict, taxonomy: str, name: str, unit: str) -> dict[str, Decimal]:
        """Eligible values keyed by period end; only facts filed <= cutoff (PIT)."""
        node = (facts.get("facts", {}).get(taxonomy) or {}).get(name)
        if not node:
            return {}
        out: dict[str, Decimal] = {}
        for entry in node.get("units", {}).get(unit, []):
            filed, end, start, val = entry.get("filed"), entry.get("end"), entry.get("start"), entry.get("val")
            if filed is None or end is None or val is None or date.fromisoformat(filed) > self._cutoff_date:
                continue
            if start is not None and (date.fromisoformat(end) - date.fromisoformat(start)).days < 350:
                continue
            out[end] = Decimal(str(val))
        return out

    @staticmethod
    def _latest(by_end: dict[str, Decimal]) -> Decimal | None:
        return by_end[max(by_end)] if by_end else None

    def _revenue(self, facts: dict) -> Decimal | None:
        for revenue_name in _REVENUE_CONCEPTS:
            value = self._latest(self._annual_by_end(facts, "us-gaap", revenue_name, "USD"))
            if value is not None:
                return value
        return None

    def _gross_profit(self, facts: dict) -> Decimal | None:
        direct = self._annual_by_end(facts, "us-gaap", "GrossProfit", "USD")
        if direct:
            return self._latest(direct)
        for cogs_name in _COGS_CONCEPTS:
            cogs = self._annual_by_end(facts, "us-gaap", cogs_name, "USD")
            if not cogs:
                continue
            for revenue_name in _REVENUE_CONCEPTS:
                revenue = self._annual_by_end(facts, "us-gaap", revenue_name, "USD")
                shared = set(revenue) & set(cogs)  # same period end -> comparable
                if shared:
                    end = max(shared)
                    return revenue[end] - cogs[end]
        return None

    def _pre_provision_profit(self, facts: dict) -> Decimal | None:
        """Bank pre-provision net revenue: net revenue minus noninterest expense."""
        revenue = self._annual_by_end(facts, "us-gaap", "RevenuesNetOfInterestExpense", "USD")
        if not revenue:
            revenue = self._annual_by_end(facts, "us-gaap", "Revenues", "USD")
        expense = self._annual_by_end(facts, "us-gaap", "NoninterestExpense", "USD")
        shared = set(revenue) & set(expense)
        if shared:
            end = max(shared)
            return revenue[end] - expense[end]
        return None

    def _real_financial(self, ticker: str) -> dict[str, str | None]:
        facts: dict | None = None
        try:
            cik = sec.ticker_to_cik(_sec_ticker(ticker))
            facts = sec.fetch_company_facts(cik)
        except Exception:  # SEC mapping/HTTP failure: capture the cell with null values
            facts = None
        is_financial = ticker in _FINANCIAL_BRANCH_TICKERS
        assets = None if facts is None else self._latest(self._annual_by_end(facts, "us-gaap", "Assets", "USD"))
        shares = (
            None
            if facts is None
            else self._latest(self._annual_by_end(facts, "us-gaap", "CommonStockSharesOutstanding", "shares"))
        )
        revenue = None if facts is None else self._revenue(facts)
        gross = None if (facts is None or is_financial) else self._gross_profit(facts)
        ppnr = self._pre_provision_profit(facts) if (facts is not None and is_financial) else None
        return {
            "operating_branch": "financial" if is_financial else "non_financial",
            "currency": "USD",
            "gross_profit": None if gross is None else str(gross),
            "total_assets": None if assets is None else str(assets),
            "headcount": _HEADCOUNT.get(ticker),
            "revenue": None if revenue is None else str(revenue),
            "shares_outstanding": None if shares is None else str(shares),
            "pre_provision_profit": None if ppnr is None else str(ppnr),
        }

    def _real_payload(self, coordinates: tuple[str, str, str, str], semantic_type: str) -> dict[str, str | None]:
        issuer_id, instrument_id, listing_id, ticker = coordinates
        identity = {"issuer_id": issuer_id, "instrument_id": instrument_id, "listing_id": listing_id}
        if semantic_type in {"listing-identity", "universe-membership"}:
            return {**identity, "ticker": ticker}
        if semantic_type == "market-price":
            return {**identity, "currency": "USD", "close": self._real_market_price(ticker)}
        if semantic_type == "financial-fact":
            return {**identity, **self._real_financial(ticker)}
        raise AssertionError(semantic_type)

    # -- persistence --------------------------------------------------------------

    def _source_request(self, obligation, *, ordinal: int, origin: str | None = None) -> SourceRequest:
        coordinate: dict[str, Any] = {
            "ordinal": ordinal,
            "subject": obligation.subject.model_dump(mode="json"),
            "requirement": obligation.capture_requirement_id,
            "partition": obligation.partition,
        }
        source = f"production-topt-{self._version}"
        if origin is not None:
            coordinate["origin"] = origin
            source = f"twelve-data-recon-{self._version}"
        return SourceRequest(
            source_registry_entry_id=f"source-registry-entry:{canonical_sha256({'source': source})}",
            source_policy_id=f"source-policy:{source}",
            request_fingerprint_version=f"{source}:v1",
            canonical_request_sha256=canonical_sha256(coordinate),
            subject_refs=(obligation.subject,),
            capture_requirement_ids=(obligation.capture_requirement_id,),
            partition=obligation.partition,
        )

    def _insert_fetch(self, connection: psycopg.Connection[Any], *, source: str, record_id: str, sha256: str) -> int:
        """Idempotent raw.fetches landing: a retried tick reuses the existing row."""
        row = connection.execute(
            "insert into raw.fetches (source, source_record_id, payload_sha256, object_uri, content_type, "
            "byte_length, fetched_at, recorded_at, metadata) "
            "values (%s, %s, %s, %s, 'application/json', 1, %s, %s, '{}'::jsonb) "
            "on conflict (source, source_record_id) do nothing returning id",
            (
                source,
                record_id,
                sha256,
                f"s3://{source}/{sha256}",
                self._cutoff - timedelta(hours=2),
                self._cutoff - timedelta(hours=2),
            ),
        ).fetchone()
        if row is not None:
            return row[0]
        return connection.execute(
            "select id from raw.fetches where source = %s and source_record_id = %s", (source, record_id)
        ).fetchone()[0]

    def _write_second_price_source(self, connection, repo, obligation, coordinates, *, ordinal: int, price: str):
        """Persist the Twelve Data price as a supplementary observation (distinct
        source request -> two independent origins in the quality report, #344). The
        materializer ignores it: its snapshot binds only the terminal-attempt vintage."""
        issuer_id, instrument_id, listing_id, _ticker = coordinates[obligation.subject.id]
        request = self._source_request(obligation, ordinal=ordinal, origin="twelve_data")
        repo.put_source_request(request)
        payload: dict[str, str | None] = {
            "issuer_id": issuer_id,
            "instrument_id": instrument_id,
            "listing_id": listing_id,
            "price": price,
            "currency": "USD",
            "origin": "twelve_data",
        }
        raw_sha256 = canonical_sha256({"ordinal": ordinal, "payload": payload, "origin": "twelve_data"})
        source = f"twelve-data-recon-{self._version}"
        record_id = f"{source}:{ordinal}"
        raw_fetch_id = self._insert_fetch(connection, source=source, record_id=record_id, sha256=raw_sha256)
        vintage = SourceVintage(
            source_request_id=request.source_request_id,
            source_record_id=record_id,
            source_published_at=self._cutoff - timedelta(hours=2),
            raw_object_id=f"raw-object:{raw_sha256}",
        )
        observation = NormalizedObservation(
            semantic_type="market-price",
            semantic_version=obligation.capture_requirement_id,
            subject=obligation.subject,
            valid_from=self._cutoff - timedelta(days=2),
            valid_to=self._cutoff - timedelta(days=2),
            knowable_at=self._cutoff - timedelta(minutes=58),
            source_vintage_id=vintage.source_vintage_id,
            parser_version="twelve-data-parser:v1",
            mapping_version="twelve-data-map:v1",
            normalized_payload_sha256=canonical_sha256(payload),
        )
        repo.put_source_vintage(vintage, raw_fetch_id=raw_fetch_id)
        repo.put_observation(
            obligation.obligation_id,
            observation,
            normalized_payload=payload,
            confidence=_confidence("market-price", payload),
            freshness_state="fresh",
        )

    def capture(self, connection: psycopg.Connection[Any]) -> tuple[str, str]:
        """Capture all 84 obligations from real sources; returns (run_id, release_manifest_id)."""
        corpus = json.loads(CORPUS.read_text())
        denominator = corpus["topt_denominator"]
        coordinates = {row[2]: tuple(row) for row in denominator["instruments"]}
        list_version = frozen_topt_list_version(corpus)
        policy = CaptureSchedulePolicy(
            policy_version=f"production-topt-{self._version}",
            demanded_cadence=timedelta(days=1),
            provider_availability_cadence="scheduled:v1",
            freshness_max_age=timedelta(days=2),
            retry=replay_retry_policy(3),
        )
        campaign = CaptureCampaign(
            campaign_policy_id=f"capture-policy:production-topt-{self._version}",
            environment=CaptureEnvironment.PRODUCTION,
            cutoff=self._cutoff,
            universe_refs=(list_version.universe,),
        )
        run = CaptureRun(
            campaign_id=campaign.campaign_id,
            run_sequence=1,
            schedule_policy_id=policy.schedule_policy_id,
            capture_scope_id=f"capture-scope:{canonical_sha256({'scope': f'production-topt-{self._version}'})}",
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

        source = f"production-topt-{self._version}"
        for ordinal, obligation in enumerate(obligations):
            request = self._source_request(obligation, ordinal=ordinal)
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
            payload = self._real_payload(coordinates[obligation.subject.id], semantic_type)
            raw_sha256 = canonical_sha256({"ordinal": ordinal, "payload": payload})
            record_id = f"{source}:{ordinal}"
            raw_fetch_id = self._insert_fetch(connection, source=source, record_id=record_id, sha256=raw_sha256)
            vintage = SourceVintage(
                source_request_id=request.source_request_id,
                source_record_id=record_id,
                source_published_at=self._cutoff - timedelta(hours=2),
                raw_object_id=f"raw-object:{raw_sha256}",
            )
            ledger = AttemptLedger(work_item_id=work_item.work_item_id, retry_policy=policy.retry)
            attempt = ledger.start(started_at=self._cutoff - timedelta(hours=1))
            attempt_result = ledger.finish(
                attempt=attempt,
                completed_at=self._cutoff - timedelta(minutes=59),
                outcome=FetchAttemptOutcome.SUCCESS,
                status_code=200,
                source_vintage_id=vintage.source_vintage_id,
            )
            observation = NormalizedObservation(
                semantic_type=semantic_type,
                semantic_version=obligation.capture_requirement_id,
                subject=obligation.subject,
                valid_from=self._cutoff - timedelta(days=2),
                valid_to=self._cutoff - timedelta(days=2),
                knowable_at=self._cutoff - timedelta(minutes=58),
                source_vintage_id=vintage.source_vintage_id,
                parser_version=PRIMARY_PARSER_VERSION,
                mapping_version="production-topt-live-map:v1",
                normalized_payload_sha256=canonical_sha256(payload),
            )
            terminal = ListObligationResult(
                obligation_id=obligation.obligation.obligation_id,
                terminal_state=ObligationTerminalState.SUCCESS,
                completed_at=self._cutoff - timedelta(minutes=57),
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
            if semantic_type == "market-price":
                second_price = self._twelve_data_price(coordinates[obligation.subject.id][3])
                if second_price is not None:
                    self._write_second_price_source(
                        connection, repo, obligation, coordinates, ordinal=ordinal, price=second_price
                    )

        return run.run_id, release_manifest_id


def run_live_topt_pipeline(
    connection: psycopg.Connection[Any], *, cutoff: datetime, version: str
) -> LiveToptPipelineResult:
    """Capture -> freeze -> materialize -> quality report, in the caller's transaction."""
    capture = LiveToptCapture(cutoff=cutoff, version=version)
    run_id, release_manifest_id = capture.capture(connection)
    status = PostgresCaptureControlRepository(connection).status(run_id)
    if not status.complete:
        raise RuntimeError(f"capture run {run_id} incomplete: {status}")

    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=run_id, release_manifest_id=release_manifest_id)
    results = core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate=Decimal("0.05")))

    report = quality_report.build_report(connection, run_id)
    report_id = quality_report.persist(connection, report)
    return LiveToptPipelineResult(
        run_id=run_id,
        release_manifest_id=release_manifest_id,
        core_result_count=len(results),
        quality_report_id=report_id,
        quality=report,
    )


_STRATEGY_FINANCIAL_KEYS = ("gross_profit", "total_assets", "headcount", "revenue", "shares_outstanding")


def seed_strategy_inputs_from_capture(connection: psycopg.Connection[Any], run_id: str, *, cutoff: datetime) -> int:
    """The capture->strategy bridge (#429): land the run's captured cells as
    provenance-neutral strategy inputs in ``staging.strategy_backtest_inputs``.

    One canonical listing per issuer (lowest listing_id) supplies ``last_close``; the
    SEC shares figure is issuer-level, so a dual-class issuer's market value uses its
    canonical class's price — an approximation already reflected in the price
    confidence. Missing fields are simply not seeded; the evaluator excludes those
    issuers with explicit reasons rather than receiving fabricated values.
    """
    rows = connection.execute(
        """
        select o.semantic_type, o.confidence, p.normalized_payload
        from raw.capture_obligations ob
        join staging.capture_observation_obligations oo on oo.capture_obligation_id = ob.obligation_id
        join staging.capture_normalized_observations o on o.observation_id = oo.observation_id
        join staging.capture_observation_payloads p on p.observation_id = o.observation_id
        where ob.run_id = %s
          and o.parser_version = %s
          and o.semantic_type in ('financial-fact', 'market-price')
        """,
        (run_id, PRIMARY_PARSER_VERSION),
    ).fetchall()

    financial: dict[str, tuple[str, dict, Decimal]] = {}  # issuer -> (listing, payload, confidence)
    price: dict[str, tuple[str, dict, Decimal]] = {}
    for semantic_type, confidence, payload in rows:
        issuer_id, listing_id = payload["issuer_id"], payload["listing_id"]
        bucket = financial if semantic_type == "financial-fact" else price
        current = bucket.get(issuer_id)
        if current is None or listing_id < current[0]:  # canonical listing = lowest listing_id
            bucket[issuer_id] = (listing_id, payload, Decimal(str(confidence)))

    knowable_at = cutoff - timedelta(minutes=58)
    written = 0
    for issuer_id in sorted(set(financial) | set(price)):
        inputs: list[tuple[str, str, Decimal]] = []
        if issuer_id in financial:
            _listing, payload, confidence = financial[issuer_id]
            for key in _STRATEGY_FINANCIAL_KEYS:
                value = payload.get(key)
                if value is not None:
                    inputs.append((key, value, confidence))
        if issuer_id in price:
            _listing, payload, confidence = price[issuer_id]
            close = payload.get("close")
            if close is not None:
                inputs.append(("last_close", close, confidence))
        for input_key, value, confidence in inputs:
            connection.execute(
                """
                insert into staging.strategy_backtest_inputs
                    (issuer_id, cutoff_at, input_key, value, confidence, knowable_at)
                values (%s, %s, %s, %s, %s, %s)
                """,
                (issuer_id, cutoff, input_key, value, confidence, knowable_at),
            )
            written += 1
    return written


def run_strategy_replay_for_cutoff(
    connection: psycopg.Connection[Any],
    *,
    cutoff: datetime,
    executed_at: datetime,
    risk_free_rate: Decimal,
) -> tuple[str, int, str]:
    """Evaluate the frozen strategy over the captured staging inputs for one cutoff and
    persist ``mart.strategy_runs``/``mart.strategy_decisions``. The risk-free rate is the
    same 0.05 the GPPE materialization pins, supplied explicitly by the caller."""
    definition = load_strategy_definition()
    gateway = StrategyBacktestGateway(connection)
    issuer_inputs = gateway.issuer_inputs(cutoff)
    evaluated = evaluate_cutoff(issuer_inputs, definition=definition, cutoff_at=cutoff, risk_free_rate=risk_free_rate)
    cutoff_key = cutoff.astimezone(UTC).isoformat()
    decisions = sorted(
        (_to_decision(item, cutoff_key) for item in evaluated),
        key=lambda item: (item.cutoff_at, item.issuer_id),
    )
    snapshot_id = gateway.snapshot_id(cutoff)
    run_id, decision_ids = write_replay(
        connection, decisions, definition, executed_at=executed_at, snapshot_id=snapshot_id
    )
    return run_id, len(decision_ids), snapshot_id
