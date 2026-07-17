"""Explicit Local/CI Dagster composition landing the sample SEC corpus into
`staging.financial_facts` (see `financial_facts_pipeline.py`'s own docstring
for the full rationale and scope).

Same shape as `headcount_assets.py`'s `H0E1RunnerResource`/
`build_h0_e1_definitions`: the connection and raw-object store are supplied
as a Dagster resource by the caller rather than opened inside the asset, so
the asset itself stays testable without real credentials, and no default
`Definitions`/schedule/release activation exists here — a caller wires this
explicitly for Local/CI.
"""

from dataclasses import dataclass
from typing import Any, cast

import dagster as dg
from dagster import AssetExecutionContext
from psycopg import Connection
from truealpha_contracts import RawObjectStore

from data_engine.financial_facts_pipeline import SAMPLE_ISSUERS, capture_and_write_all

FINANCIAL_FACTS_ASSET_NAME = "sample_financial_facts_capture"


@dataclass(frozen=True)
class FinancialFactsRunnerResource:
    connection: Connection[Any]
    raw_store: RawObjectStore

    def run(self) -> dict[str, tuple[str, ...]]:
        return capture_and_write_all(self.connection, raw_store=self.raw_store)


@dg.asset(
    name=FINANCIAL_FACTS_ASSET_NAME,
    group_name="financial_facts_capture",
    required_resource_keys={"financial_facts_runner"},
    description=(
        "Capture the checked-in SEC company-facts sample corpus through raw.fetches and land "
        "total_assets/gross_profit/revenue as staging.financial_facts vintages. Local/CI only, "
        "fixture-sourced -- see the module docstring for scope."
    ),
)
def materialize_sample_financial_facts_capture(context: AssetExecutionContext) -> dg.Output[dict[str, list[str]]]:
    runner = cast(FinancialFactsRunnerResource, context.resources.financial_facts_runner)
    written = runner.run()
    total_metrics = sum(len(metrics) for metrics in written.values())
    context.log.info(f"Landed {total_metrics} financial facts across {len(written)} sample issuers")
    return dg.Output(
        {ticker: list(metrics) for ticker, metrics in written.items()},
        metadata={
            "issuer_count": len(written),
            "metric_count": total_metrics,
            "issuers": ", ".join(sorted(written)),
        },
    )


def build_financial_facts_definitions(*, connection: Connection[Any], raw_store: RawObjectStore) -> dg.Definitions:
    return dg.Definitions(
        assets=[materialize_sample_financial_facts_capture],
        resources={
            "financial_facts_runner": cast(
                Any, FinancialFactsRunnerResource(connection=connection, raw_store=raw_store)
            )
        },
    )


__all__ = [
    "FINANCIAL_FACTS_ASSET_NAME",
    "FinancialFactsRunnerResource",
    "SAMPLE_ISSUERS",
    "build_financial_facts_definitions",
    "materialize_sample_financial_facts_capture",
]
