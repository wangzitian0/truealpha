"""Normalize the bounded SEC companyfacts metrics required by capture v1."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from data_engine import raw_store
from data_engine.normalizers import lineage

MAPPING_VERSION = "sec-companyfacts:1"
SUPPORTED_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}

# Earlier tags win only for an otherwise identical context. The normalizer
# preserves source_metric so a registry revision remains attributable.
METRIC_CONCEPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": (
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "SalesRevenueNet"),
        ("us-gaap", "SalesRevenueGoodsNet"),
    ),
    "cost_of_revenue": (
        ("us-gaap", "CostOfRevenue"),
        ("us-gaap", "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"),
        ("us-gaap", "CostOfGoodsSold"),
    ),
    "gross_profit": (("us-gaap", "GrossProfit"),),
    "operating_income": (("us-gaap", "OperatingIncomeLoss"),),
    "net_income": (
        ("us-gaap", "NetIncomeLoss"),
        ("us-gaap", "ProfitLoss"),
        ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic"),
    ),
    "shares_outstanding": (
        ("us-gaap", "CommonStockSharesOutstanding"),
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
        ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
    ),
    "eps_diluted": (("us-gaap", "EarningsPerShareDiluted"),),
}
METRIC_TAGS = {metric: tuple(tag for _taxonomy, tag in concepts) for metric, concepts in METRIC_CONCEPTS.items()}


@dataclass(frozen=True)
class NormalizedFact:
    metric: str
    value: Decimal
    unit: str
    fiscal_period: str
    valid_from: date
    valid_to: date
    knowable_at: datetime
    source_metric: str
    accession: str
    form: str
    is_restatement: bool


def _knowable_at(filed: str) -> datetime:
    # Companyfacts exposes only a filing date, not an acceptance timestamp.
    # Next-day 00:00 UTC is a conservative boundary that cannot make the fact
    # visible before any filing published on that calendar date.
    return datetime.combine(date.fromisoformat(filed) + timedelta(days=1), time.min, tzinfo=UTC)


def _validate_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("SEC companyfacts payload must be an object")
    if not isinstance(payload.get("cik"), int) or not isinstance(payload.get("entityName"), str):
        raise ValueError("SEC companyfacts payload lost cik/entityName")
    facts = payload.get("facts")
    if not isinstance(facts, dict) or not isinstance(facts.get("us-gaap"), dict):
        raise ValueError("SEC companyfacts payload lost facts.us-gaap")
    return payload


def parse(payload: dict[str, Any]) -> tuple[NormalizedFact, ...]:
    payload = _validate_payload(payload)
    taxonomies = payload["facts"]
    candidates: list[tuple[int, NormalizedFact]] = []
    for metric, concepts in METRIC_CONCEPTS.items():
        for tag_rank, (taxonomy_name, tag) in enumerate(concepts):
            taxonomy = taxonomies.get(taxonomy_name, {})
            concept = taxonomy.get(tag)
            if not isinstance(concept, dict):
                continue
            units = concept.get("units")
            if not isinstance(units, dict):
                raise ValueError(f"SEC concept {tag} lost units")
            for unit, observations in units.items():
                if not isinstance(observations, list):
                    raise ValueError(f"SEC concept {tag}.{unit} observations must be a list")
                for observation in observations:
                    if not isinstance(observation, dict) or observation.get("form") not in SUPPORTED_FORMS:
                        continue
                    required = ("val", "end", "filed", "accn", "fy", "fp", "form")
                    if any(observation.get(field) is None for field in required):
                        continue
                    end = date.fromisoformat(str(observation["end"]))
                    start = date.fromisoformat(str(observation.get("start") or observation["end"]))
                    value = Decimal(str(observation["val"]))
                    fiscal_period = f"FY{observation['fy']}:{observation['fp']}:{start.isoformat()}:{end.isoformat()}"
                    candidates.append(
                        (
                            tag_rank,
                            NormalizedFact(
                                metric=metric,
                                value=value,
                                unit=str(unit),
                                fiscal_period=fiscal_period,
                                valid_from=start,
                                valid_to=end,
                                knowable_at=_knowable_at(str(observation["filed"])),
                                source_metric=f"{taxonomy_name}:{tag}",
                                accession=str(observation["accn"]),
                                form=str(observation["form"]),
                                is_restatement=False,
                            ),
                        )
                    )

    # Keep one source tag for an identical metric/context/accession. This avoids
    # double counting issuer aliases such as Revenues and SalesRevenueNet while
    # retaining every filing vintage and fiscal context.
    selected: dict[tuple, tuple[int, NormalizedFact]] = {}
    for tag_rank, fact in candidates:
        key = (
            fact.metric,
            fact.fiscal_period,
            fact.unit,
            fact.knowable_at,
            fact.accession,
            fact.form,
        )
        current = selected.get(key)
        if current is None or tag_rank < current[0]:
            selected[key] = (tag_rank, fact)

    ordered = sorted(
        (entry[1] for entry in selected.values()),
        key=lambda fact: (
            fact.metric,
            fact.valid_from,
            fact.valid_to,
            fact.unit,
            fact.knowable_at,
            fact.accession,
        ),
    )
    previous: dict[tuple, Decimal] = {}
    result: list[NormalizedFact] = []
    for fact in ordered:
        period_key = (fact.metric, fact.valid_from, fact.valid_to, fact.unit)
        prior_value = previous.get(period_key)
        result.append(
            NormalizedFact(
                metric=fact.metric,
                value=fact.value,
                unit=fact.unit,
                fiscal_period=fact.fiscal_period,
                valid_from=fact.valid_from,
                valid_to=fact.valid_to,
                knowable_at=fact.knowable_at,
                source_metric=fact.source_metric,
                accession=fact.accession,
                form=fact.form,
                is_restatement=prior_value is not None and prior_value != fact.value,
            )
        )
        previous[period_key] = fact.value
    return tuple(result)


def normalize_fetch(
    conn, *, raw_fetch_id: int, issuer_id: str, issuer_category: str = "unclassified"
) -> tuple[int, ...]:
    payload = json.loads(raw_store.get_payload(conn, raw_fetch_id))
    facts = parse(payload)
    raw_ref = raw_store.raw_ref(raw_fetch_id)
    recorded_at = datetime.now(UTC)
    record_ids: list[int] = []
    for fact in facts:
        row = conn.execute(
            """
            insert into staging.financial_facts
                (unified_id, metric, fiscal_period, valid_time, transaction_time,
                 recorded_at, value, confidence, source, raw_ref, is_restatement,
                 unit, source_metric, accession, form, mapping_version, issuer_category)
            values (%s, %s, %s, daterange(%s::date, (%s::date + 1), '[)'), %s,
                    %s, %s, 1, 'sec', %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict do nothing returning id
            """,
            (
                issuer_id,
                fact.metric,
                fact.fiscal_period,
                fact.valid_from,
                fact.valid_to,
                fact.knowable_at,
                recorded_at,
                fact.value,
                raw_ref,
                fact.is_restatement,
                fact.unit,
                fact.source_metric,
                fact.accession,
                fact.form,
                MAPPING_VERSION,
                issuer_category,
            ),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                select id from staging.financial_facts
                where unified_id = %s and metric = %s and fiscal_period = %s
                  and transaction_time = %s and source = 'sec' and raw_ref = %s
                  and mapping_version = %s
                """,
                (issuer_id, fact.metric, fact.fiscal_period, fact.knowable_at, raw_ref, MAPPING_VERSION),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"could not persist or recover normalized SEC fact {issuer_id}/{fact.metric}")
        record_ids.append(row[0])
        lineage.link(
            conn,
            table="financial_facts",
            record_id=row[0],
            raw_ref=raw_ref,
            mapping_version=MAPPING_VERSION,
        )
    return tuple(sorted(set(record_ids)))
