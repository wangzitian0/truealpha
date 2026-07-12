"""The #27/#51 TOPT Staging canary as an immutable capture scope."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from truealpha_contracts import (
    CaptureCellRequirement,
    CaptureEnvironment,
    CaptureRequirementLevel,
    CaptureScope,
    CaptureSubject,
    CaptureSubjectKind,
    DataDomain,
    DataSource,
    canonical_sha256,
)

TOPT_FUND_ID = "etf:series:S000088434"
TOPT_BASELINE_ACCESSION = "000207169126012475"
TOPT_BASELINE_REPORT_PERIOD = "2026-03-31"
TOPT_BASELINE_KNOWABLE_AT = datetime(2026, 5, 28, tzinfo=UTC)


@dataclass(frozen=True)
class ToptInstrument:
    issuer_cik: int
    issuer_name: str
    ticker: str
    moomoo_code: str
    cusip: str
    isin: str

    @property
    def issuer_id(self) -> str:
        return f"company:cik:{self.issuer_cik}"

    @property
    def instrument_id(self) -> str:
        return f"instrument:isin:{self.isin}"


# The selected U.S. equity lines in the named N-PORT baseline. Alphabet's two
# share classes intentionally share one issuer and remain separate instruments.
TOPT_INSTRUMENTS = (
    ToptInstrument(1045810, "NVIDIA Corp.", "NVDA", "US.NVDA", "67066G104", "US67066G1040"),
    ToptInstrument(320193, "Apple, Inc.", "AAPL", "US.AAPL", "037833100", "US0378331005"),
    ToptInstrument(1652044, "Alphabet, Inc.", "GOOGL", "US.GOOGL", "02079K305", "US02079K3059"),
    ToptInstrument(789019, "Microsoft Corp.", "MSFT", "US.MSFT", "594918104", "US5949181045"),
    ToptInstrument(1652044, "Alphabet, Inc.", "GOOG", "US.GOOG", "02079K107", "US02079K1079"),
    ToptInstrument(1018724, "Amazon.com, Inc.", "AMZN", "US.AMZN", "023135106", "US0231351067"),
    ToptInstrument(1067983, "Berkshire Hathaway, Inc.", "BRK-B", "US.BRK.B", "084670702", "US0846707026"),
    ToptInstrument(1318605, "Tesla, Inc.", "TSLA", "US.TSLA", "88160R101", "US88160R1014"),
    ToptInstrument(1730168, "Broadcom, Inc.", "AVGO", "US.AVGO", "11135F101", "US11135F1012"),
    ToptInstrument(1326801, "Meta Platforms, Inc.", "META", "US.META", "30303M102", "US30303M1027"),
    ToptInstrument(19617, "JPMorgan Chase & Co.", "JPM", "US.JPM", "46625H100", "US46625H1005"),
    ToptInstrument(59478, "Eli Lilly & Co.", "LLY", "US.LLY", "532457108", "US5324571083"),
    ToptInstrument(34088, "Exxon Mobil Corp.", "XOM", "US.XOM", "30231G102", "US30231G1022"),
    ToptInstrument(200406, "Johnson & Johnson", "JNJ", "US.JNJ", "478160104", "US4781601046"),
    ToptInstrument(104169, "Walmart, Inc.", "WMT", "US.WMT", "931142103", "US9311421039"),
    ToptInstrument(1403161, "Visa, Inc.", "V", "US.V", "92826C839", "US92826C8394"),
    ToptInstrument(909832, "Costco Wholesale Corp.", "COST", "US.COST", "22160K105", "US22160K1051"),
    ToptInstrument(1141391, "Mastercard, Inc.", "MA", "US.MA", "57636Q104", "US57636Q1040"),
    ToptInstrument(1065280, "Netflix, Inc.", "NFLX", "US.NFLX", "64110L106", "US64110L1061"),
    ToptInstrument(1551152, "AbbVie, Inc.", "ABBV", "US.ABBV", "00287Y109", "US00287Y1091"),
    ToptInstrument(723125, "Micron Technology, Inc.", "MU", "US.MU", "595112103", "US5951121038"),
)


@dataclass(frozen=True)
class DomainPolicy:
    domain: DataDomain
    fields: tuple[str, ...]
    primary_source: DataSource
    fallback_sources: tuple[DataSource, ...] = ()
    maximum_age: timedelta | None = None
    minimum_confidence: Decimal = Decimal("0.8")


FUND_POLICIES = (
    DomainPolicy(
        DataDomain.FUND_HOLDINGS,
        ("instrument_id", "report_period", "knowable_at", "value", "weight", "currency"),
        DataSource.NPORT,
        maximum_age=timedelta(days=120),
        minimum_confidence=Decimal("0.9"),
    ),
    DomainPolicy(
        DataDomain.UNIVERSE,
        ("issuer_id", "instrument_id", "valid_from", "knowable_at"),
        DataSource.NPORT,
        maximum_age=timedelta(days=120),
        minimum_confidence=Decimal("0.9"),
    ),
)

ISSUER_POLICIES = (
    DomainPolicy(
        DataDomain.ENTITY_IDENTITY,
        ("cik", "issuer_name"),
        DataSource.SEC,
        (DataSource.OPENFIGI,),
        minimum_confidence=Decimal("0.9"),
    ),
    DomainPolicy(
        DataDomain.FINANCIAL_FACTS,
        ("revenue", "gross_profit", "net_income", "shares_outstanding", "fiscal_period", "unit"),
        DataSource.SEC,
        (DataSource.MOOMOO,),
        maximum_age=timedelta(days=550),
        minimum_confidence=Decimal("0.9"),
    ),
    DomainPolicy(
        DataDomain.FORECASTS,
        ("metric", "forecast_period", "estimate", "currency", "knowable_at"),
        DataSource.MOOMOO,
        maximum_age=timedelta(days=45),
    ),
    DomainPolicy(
        DataDomain.COMPANY_GUIDANCE,
        ("metric", "forecast_period", "range_low", "range_high", "unit", "published_at"),
        DataSource.SEC,
        maximum_age=timedelta(days=550),
        minimum_confidence=Decimal("0.9"),
    ),
    DomainPolicy(
        DataDomain.FILINGS,
        ("accession", "form", "period", "published_at", "document_sha256"),
        DataSource.SEC,
        maximum_age=timedelta(days=550),
        minimum_confidence=Decimal("1"),
    ),
    DomainPolicy(
        DataDomain.FILING_EXTRACTIONS,
        ("semantic_record_id", "evidence_span", "extractor_version", "review_state"),
        DataSource.SEC,
        maximum_age=timedelta(days=550),
    ),
    DomainPolicy(
        DataDomain.ANALYST_RATINGS,
        ("analyst_id", "action", "rating", "recommendation_at", "knowable_at"),
        DataSource.MOOMOO,
        maximum_age=timedelta(days=45),
    ),
    DomainPolicy(
        DataDomain.SEGMENTS,
        ("segment", "revenue", "period", "taxonomy_version"),
        DataSource.MOOMOO,
        (DataSource.SEC,),
        maximum_age=timedelta(days=550),
    ),
    DomainPolicy(
        DataDomain.KNOWLEDGE_GRAPH,
        ("counterparty_id", "relation_type", "valid_from", "evidence_span"),
        DataSource.NPORT,
        (DataSource.SEC,),
        maximum_age=timedelta(days=550),
    ),
)

INSTRUMENT_POLICIES = (
    DomainPolicy(
        DataDomain.INSTRUMENTS,
        ("issuer_id", "isin", "cusip", "ticker", "listing", "currency"),
        DataSource.OPENFIGI,
        (DataSource.SEC,),
        minimum_confidence=Decimal("0.9"),
    ),
    DomainPolicy(
        DataDomain.MARKET_PRICES,
        ("open", "high", "low", "close", "adjusted_close", "volume", "currency"),
        DataSource.TWELVE_DATA,
        (DataSource.YAHOO,),
        maximum_age=timedelta(days=7),
        minimum_confidence=Decimal("0.8"),
    ),
    DomainPolicy(
        DataDomain.CORPORATE_ACTIONS,
        ("action_type", "knowable_at", "effective_date", "ratio", "cash_amount", "currency"),
        DataSource.MOOMOO,
        (DataSource.YAHOO,),
        maximum_age=timedelta(days=550),
        minimum_confidence=Decimal("0.8"),
    ),
)


def _subjects() -> tuple[CaptureSubject, ...]:
    fund = CaptureSubject(
        subject_id=TOPT_FUND_ID,
        display_name="iShares Top 20 U.S. Stocks ETF",
        kind=CaptureSubjectKind.FUND,
        identifiers={
            "ticker": "TOPT",
            "sec_series": "S000088434",
            "baseline_accession": TOPT_BASELINE_ACCESSION,
        },
    )
    issuers = {
        item.issuer_id: CaptureSubject(
            subject_id=item.issuer_id,
            display_name=item.issuer_name,
            kind=CaptureSubjectKind.ISSUER,
            identifiers={"cik": str(item.issuer_cik)},
        )
        for item in TOPT_INSTRUMENTS
    }
    instruments = tuple(
        CaptureSubject(
            subject_id=item.instrument_id,
            display_name=f"{item.issuer_name} ({item.ticker})",
            kind=CaptureSubjectKind.INSTRUMENT,
            parent_subject_id=item.issuer_id,
            identifiers={
                "cusip": item.cusip,
                "isin": item.isin,
                "moomoo": item.moomoo_code,
                "ticker": item.ticker,
            },
        )
        for item in TOPT_INSTRUMENTS
    )
    return (fund, *issuers.values(), *instruments)


def _requirements(subjects: tuple[CaptureSubject, ...], partition_key: str) -> tuple[CaptureCellRequirement, ...]:
    policies = {
        CaptureSubjectKind.FUND: FUND_POLICIES,
        CaptureSubjectKind.ISSUER: ISSUER_POLICIES,
        CaptureSubjectKind.INSTRUMENT: INSTRUMENT_POLICIES,
    }
    return tuple(
        CaptureCellRequirement(
            subject_id=subject.subject_id,
            domain=policy.domain,
            partition_key=partition_key,
            level=CaptureRequirementLevel.REQUIRED,
            required_fields=policy.fields,
            primary_source=policy.primary_source,
            fallback_sources=policy.fallback_sources,
            maximum_age=policy.maximum_age,
            minimum_confidence=policy.minimum_confidence,
        )
        for subject in subjects
        for policy in policies[subject.kind]
    )


def build_topt_scope(
    *,
    as_of: datetime = TOPT_BASELINE_KNOWABLE_AT,
    approved_by: str = "issue:68",
) -> CaptureScope:
    subjects = _subjects()
    membership = [
        {
            "subject_id": subject.subject_id,
            "kind": subject.kind.value,
            "parent_subject_id": subject.parent_subject_id,
            "identifiers": subject.identifiers,
        }
        for subject in subjects
    ]
    return CaptureScope(
        scope_version="topt-staging:v1",
        environment=CaptureEnvironment.STAGING,
        research_catalog_version="research-catalog:v1",
        source_matrix_version="capture-sources:v1",
        slo_version="capture-slo:v1",
        universe_id=TOPT_FUND_ID,
        universe_version=TOPT_BASELINE_REPORT_PERIOD,
        universe_membership_sha256=canonical_sha256(membership),
        as_of=as_of,
        approved_by=approved_by,
        subjects=subjects,
        requirements=_requirements(subjects, f"as-of:{as_of.isoformat()}"),
    )
