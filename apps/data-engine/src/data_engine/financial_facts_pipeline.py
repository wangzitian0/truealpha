"""Land the checked-in SEC company-facts corpus into `staging.financial_facts`.

Wires three previously-separate pieces together for the first time: the
generalized SEC XBRL extraction layer (`sec_financial_facts.py`), the
immutable raw-capture ledger (`raw_store.py`), and the SSOT staging writer
(`financial_facts_repository.py`). Each of those modules' own docstrings
named this exact gap as "separate, larger follow-up work" -- this is that
follow-up.

Fixture-sourced like `headcount_pipeline.py`'s `HeadcountCorpusAdapter`: the
company-facts bytes are frozen reconnaissance captures under
`apps/data-engine/samples/sec/` (`apps/data-engine/samples/README.md`), not a
live SEC pull, but they land through the same `raw.fetches` ledger a live
sweep would use (`metadata={"fixture_only": True}` distinguishes the
provenance, not a separate code path) -- matching that module's precedent
rather than `core_strategy_replay_assets.py`'s (which skips the ledger
entirely as a pure preview). Local/CI only, like every fixture-sourced
pipeline in this repo; no default schedule or Staging route.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import Connection
from truealpha_contracts import DataSource, RawObjectStore
from truealpha_contracts.models import FinancialFact

from data_engine.financial_facts_repository import ensure_kg_entity, write_financial_fact
from data_engine.raw_store import get_payload, insert_json_fetch, raw_ref
from data_engine.sec_financial_facts import (
    SecFinancialObservation,
    extract_gross_profit,
    extract_revenue,
    extract_total_assets,
)

SAMPLES_DIR = Path("apps/data-engine/samples/sec")

# apps/data-engine/samples/capture_manifest_20260712.json: completed_at. The
# checked-in bytes are a frozen reconnaissance capture, not a live pull, so
# this pipeline's own "fetched_at" reuses that grounded timestamp rather than
# wall-clock now() -- HeadcountCorpusAdapter.capture() sets the same
# precedent for reviewed local bytes.
CAPTURE_FETCHED_AT = datetime(2026, 7, 11, 19, 4, 54, tzinfo=UTC)

# "Everything filed in the corpus so far is visible" -- the same
# far-future-cutoff convention test_sec_financial_facts.py uses. This
# pipeline lands the latest annual value per metric as one vintage; it does
# not attempt a full historical PIT series in one pass (extract_annual_metric
# itself only ever selects one row per call). A future consumer replaying a
# specific historical cutoff filters staging rows on transaction_time, the
# same as any other staging.financial_facts reader would.
EXTRACTION_CUTOFF = datetime(2099, 1, 1, tzinfo=UTC)

MAPPING_VERSION = "sec-companyfacts:1"

SAMPLE_ISSUERS: dict[str, str] = {
    "adm": "ADM_CIK0000007084.json",
    "ddog": "DDOG_CIK0001561550.json",
    "duol": "DUOL_CIK0001562088.json",
    "jpm": "JPM_CIK0000019617.json",
    "meta": "META_CIK0001326801.json",
    "nice": "NICE_CIK0001003935.json",
    "nvda": "NVDA_CIK0001045810.json",
    "plug": "PLUG_CIK0001093691.json",
    "shop": "SHOP_CIK0001594805.json",
}

_DISPLAY_NAMES: dict[str, str] = {
    "adm": "Archer-Daniels-Midland Company",
    "ddog": "Datadog, Inc.",
    "duol": "Duolingo, Inc.",
    "jpm": "JPMorgan Chase & Co.",
    "meta": "Meta Platforms, Inc.",
    "nice": "NICE Ltd.",
    "nvda": "NVIDIA Corporation",
    "plug": "Plug Power Inc.",
    "shop": "Shopify Inc.",
}


def _company_facts(ticker: str) -> dict[str, Any]:
    path = SAMPLES_DIR / SAMPLE_ISSUERS[ticker]
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise ValueError(f"{ticker}: company-facts sample must decode to a JSON object")
    return payload


def _cik(ticker: str) -> str:
    filename = SAMPLE_ISSUERS[ticker]
    return filename.split("_CIK", 1)[1].removesuffix(".json")


def _to_financial_fact(observation: SecFinancialObservation, *, source_raw_ref: str) -> FinancialFact:
    return FinancialFact(
        entity_id=observation.entity_id,
        metric=observation.metric,
        value=observation.value,
        unit=observation.unit,
        fiscal_period=observation.fiscal_period,
        valid_from=observation.valid_from,
        valid_to=observation.valid_to,
        knowable_at=observation.knowable_at,
        recorded_at=CAPTURE_FETCHED_AT,
        confidence=observation.confidence,
        raw_ref=source_raw_ref,
        source_metric=observation.metric,
        mapping_version=MAPPING_VERSION,
        accession=observation.accession,
        form=observation.form,
    )


def capture_and_write_issuer(
    connection: Connection[Any], ticker: str, *, raw_store: RawObjectStore | None = None
) -> tuple[str, ...]:
    """Capture one issuer's company-facts bytes through `raw.fetches`, extract
    total_assets/gross_profit/revenue, and write each as a
    `staging.financial_facts` vintage. Returns the metrics actually written —
    a metric genuinely absent from the filing (e.g. `gross_profit` for a
    bank) is skipped, never fabricated."""

    entity_id = f"issuer.{ticker}"
    company_facts = _company_facts(ticker)
    cik = _cik(ticker)

    fetch_id = insert_json_fetch(
        connection,
        source=DataSource.SEC,
        source_record_id=f"companyfacts:{cik}",
        payload=company_facts,
        fetched_at=CAPTURE_FETCHED_AT,
        metadata={"fixture_only": True, "cik": cik, "ticker": ticker.upper()},
        store=raw_store,
    )
    landed = json.loads(get_payload(connection, fetch_id, store=raw_store))
    if landed != company_facts:
        raise ValueError(f"{ticker}: raw readback drifted from the sample corpus")
    fact_raw_ref = raw_ref(fetch_id)

    ensure_kg_entity(connection, entity_id=entity_id, display_name=_DISPLAY_NAMES[ticker])

    written: list[str] = []
    for extractor in (extract_total_assets, extract_gross_profit, extract_revenue):
        observation = extractor(company_facts, entity_id=entity_id, cutoff=EXTRACTION_CUTOFF)
        if observation is None:
            continue
        fact = _to_financial_fact(observation, source_raw_ref=fact_raw_ref)
        write_financial_fact(connection, fact, source=DataSource.SEC.value)
        written.append(observation.metric)
    return tuple(written)


def capture_and_write_all(
    connection: Connection[Any], *, raw_store: RawObjectStore | None = None
) -> dict[str, tuple[str, ...]]:
    return {ticker: capture_and_write_issuer(connection, ticker, raw_store=raw_store) for ticker in SAMPLE_ISSUERS}
