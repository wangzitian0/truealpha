"""Focused Wave 3 verification tests for metadata, leaves, and confidence calibration (#1851)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from data_engine.datahub._helpers import calibrated_confidence_for
from data_engine.datahub.capture_core.issuer_registry import (
    IssuerClassification,
    RegistryReading,
    resolve_issuer_classifications,
)
from data_engine.datahub.capture_core.materialization import PostgresCoreRepository
from data_engine.datahub.capture_core.metric_node_values import _materialize_panel_leaves
from data_engine.datahub.capture_core.sec_financial_adapter import (
    _companyfacts_headcount,
    gross_profit,
)
from data_engine.datahub.share_basis import SHARES_KEY
from factors.panel import GPPE_V0_SPEC, PANEL_METRICS, IssuerClass
from factors.snapshot_inputs import (
    CoreSnapshotInput,
    GppeV0Definition,
    MetricFreshness,
)
from truealpha_contracts.execution import InputEvidenceStatus


def test_calibrated_confidence_for_routes_through_cell_confidence_evaluator() -> None:
    cal = calibrated_confidence_for(
        "headcount-confidence:v1",
        "ixbrl:dei:EntityNumberOfEmployees",
        case_id="hc:320193:acc",
        evidence_id="accession:000032019326000010",
    )
    assert Decimal("0") < cal < Decimal("0.95")


def test_sec_companyfacts_headcount_and_cogs_plus_services_gross_profit() -> None:
    cutoff = date(2026, 5, 1)
    facts: dict[str, Any] = {
        "facts": {
            "dei": {
                "EntityNumberOfEmployees": {
                    "units": {
                        "pure": [
                            {
                                "val": 8500,
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                                "accn": "000111111126000001",
                                "form": "10-K",
                            },
                            {
                                "val": 9999,
                                "end": "2026-06-30",
                                "filed": "2026-08-01",
                                "accn": "000111111126000009",
                                "form": "10-K",
                            },
                        ]
                    }
                }
            },
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "val": 1000,
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                                "accn": "000111111126000001",
                                "form": "10-K",
                                "fp": "FY",
                            }
                        ]
                    }
                },
                "CostOfGoodsSold": {
                    "units": {
                        "USD": [
                            {
                                "val": 300,
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                                "accn": "000111111126000001",
                                "form": "10-K",
                                "fp": "FY",
                            }
                        ]
                    }
                },
                "CostOfServices": {
                    "units": {
                        "USD": [
                            {
                                "val": 250,
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "filed": "2026-02-20",
                                "accn": "000111111126000001",
                                "form": "10-K",
                                "fp": "FY",
                            }
                        ]
                    }
                },
            },
        }
    }
    hc = _companyfacts_headcount(facts, cutoff)
    assert hc is not None
    assert hc[0] == Decimal("8500")
    gp = gross_profit(facts, cutoff)
    assert gp is not None
    assert gp.value == Decimal("450")


def test_issuer_registry_extracts_profile_fields(monkeypatch: Any) -> None:
    submissions = {
        "sic": "3571",
        "sicDescription": "Electronic Computers",
        "category": "Large accelerated filer",
        "stateOfIncorporation": "CA",
        "fiscalYearEnd": "0930",
        "exchanges": ["Nasdaq"],
        "entityType": "operating",
        "filings": {"recent": {"form": ["10-K"], "filingDate": ["2026-01-15"], "reportDate": ["2025-12-31"]}},
    }
    from contextlib import nullcontext
    from types import SimpleNamespace

    from data_engine.datahub.capture_core import issuer_registry

    monkeypatch.setattr(issuer_registry.sec, "client", lambda *_a, **_k: nullcontext(SimpleNamespace(timeout=None)))
    monkeypatch.setattr(issuer_registry, "_read_submissions", lambda *_a, **_k: issuer_registry._Read(submissions))
    reading: RegistryReading = resolve_issuer_classifications({"AAPL": 320193})
    cls: IssuerClassification = reading.classifications[320193]
    assert cls.sic == "3571"
    assert cls.sic_description == "Electronic Computers"
    assert cls.primary_exchange == "Nasdaq"
    assert cls.fiscal_year_end == "0930"
    assert cls.state_of_incorporation == "CA"


def test_shares_confidence_is_decoupled_from_financial_cell_confidence(monkeypatch: Any) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        PostgresCoreRepository,
        "_resolve_shares_on_close_basis",
        staticmethod(lambda *_a, **_k: (Decimal("1000"), datetime(2026, 2, 1, tzinfo=UTC), None)),
    )

    def _obs(oid: str, conf: Decimal, know: datetime, payload: dict[str, Any]) -> Any:
        return SimpleNamespace(
            observation_id=oid,
            content_sha256="a" * 64,
            source_vintage_id="vintage:1",
            confidence=conf,
            freshness=MetricFreshness.FRESH,
            valid_from=know,
            valid_to=None,
            knowable_at=know,
            recorded_at=know,
            payload=payload,
        )

    coords = {"issuer_id": "issuer:a", "instrument_id": "inst:a", "listing_id": "listing:xnas:aapl"}
    by_type = {
        "financial-fact": _obs(
            f"normalized-observation:{'1' * 64}",
            Decimal("0.92"),
            datetime(2026, 2, 1, tzinfo=UTC),
            {
                **coords,
                "operating_branch": "non_financial",
                "currency": "USD",
                "gross_profit": "500",
                "total_assets": "1000",
                "headcount": "10",
                "revenue": "900",
                "shares_outstanding": "1000",
                "pre_provision_profit": None,
                "operating_period_end": "2025-12-31",
                "revenue_period_end": "2025-12-31",
            },
        ),
        "market-price": _obs(
            f"normalized-observation:{'2' * 64}",
            Decimal("0.95"),
            datetime(2026, 3, 1, tzinfo=UTC),
            {**coords, "close": "50", "currency": "USD"},
        ),
        "listing-identity": _obs(
            f"normalized-observation:{'3' * 64}",
            Decimal("0.99"),
            datetime(2026, 1, 1, tzinfo=UTC),
            {**coords, "ticker": "AAPL"},
        ),
        "universe-membership": _obs(
            f"normalized-observation:{'4' * 64}",
            Decimal("0.99"),
            datetime(2026, 1, 1, tzinfo=UTC),
            {**coords, "ticker": "AAPL"},
        ),
    }
    member = PostgresCoreRepository._snapshot_member(
        "listing:xnas:aapl",
        by_type,  # type: ignore[arg-type]
        histories=(),
        listing_confidences={"gross_profit": Decimal("0.92"), "revenue": Decimal("0.91"), SHARES_KEY: Decimal("0.35")},
    )
    assert member.cell_inputs[0].confidence == Decimal("0.91")
    assert member.gross_profit is not None
    assert member.gross_profit.confidence == Decimal("0.92")
    assert member.shares_outstanding is not None
    assert member.shares_outstanding.confidence == Decimal("0.35")


def test_panel_leaves_and_market_cap_materialize_without_mutating_gppe_sha(monkeypatch: Any) -> None:
    assert "revenue" in PANEL_METRICS
    assert "market_cap" in PANEL_METRICS
    assert GPPE_V0_SPEC.pinned_sha256 == "9015a3713fd159cf5ba9014eb6ecbb14551da177c14bbd5d7acc17b1e2177906"

    inserted: list[tuple[str, Any, Decimal]] = []

    class _FakeConn:
        def execute(self, _sql: str, params: tuple[Any, ...]) -> Any:
            inserted.append((str(params[3]), params[6], params[11]))

            class _Res:
                def fetchone(self) -> tuple[Any]:
                    return (params[2],)

            return _Res()

    cutoff = datetime(2026, 3, 1, tzinfo=UTC)
    obs_fin, obs_mkt, obs_id, obs_univ = (f"normalized-observation:{ch * 64}" for ch in ("1", "2", "3", "4"))

    def _obs(oid: str, conf: Decimal, know: datetime, payload: dict[str, Any]) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            observation_id=oid,
            content_sha256="a" * 64,
            source_vintage_id="vintage:1",
            confidence=conf,
            freshness=MetricFreshness.FRESH,
            valid_from=know,
            valid_to=None,
            knowable_at=know,
            recorded_at=know,
            payload=payload,
        )

    from data_engine.datahub.capture_core.materialization import CoreSnapshot

    monkeypatch.setattr(
        PostgresCoreRepository,
        "_resolve_shares_on_close_basis",
        staticmethod(lambda *_a, **_k: (Decimal("100"), datetime(2026, 2, 1, tzinfo=UTC), None)),
    )
    coords = {"issuer_id": "issuer:a", "instrument_id": "inst:a", "listing_id": "listing:xnas:a"}
    by_type = {
        "financial-fact": _obs(
            obs_fin,
            Decimal("0.90"),
            datetime(2026, 2, 1, tzinfo=UTC),
            {
                **coords,
                "operating_branch": "non_financial",
                "currency": "USD",
                "gross_profit": "500",
                "total_assets": "1000",
                "headcount": "10",
                "revenue": "900",
                "shares_outstanding": "100",
                "pre_provision_profit": None,
                "operating_period_end": "2025-12-31",
                "revenue_period_end": "2025-12-31",
            },
        ),
        "market-price": _obs(obs_mkt, Decimal("0.95"), cutoff, {**coords, "close": "20", "currency": "USD"}),
        "listing-identity": _obs(obs_id, Decimal("0.99"), cutoff, {**coords, "ticker": "A"}),
        "universe-membership": _obs(obs_univ, Decimal("0.99"), cutoff, {**coords, "ticker": "A"}),
    }
    member = PostgresCoreRepository._snapshot_member(
        "listing:xnas:a",
        by_type,  # type: ignore[arg-type]
        histories=(),
        listing_confidences={"revenue": Decimal("0.90"), SHARES_KEY: Decimal("0.75"), "close": Decimal("0.95")},
    )
    snapshot = CoreSnapshot(
        run_id=f"capture-run:{'1' * 64}",
        release_manifest_id=f"release-manifest:{'2' * 64}",
        universe_id="universe:topt-us-2026-06-30",
        universe_version="2026-06-30",
        universe_sha256="3" * 64,
        cutoff=cutoff,
        members=(member,),
    )
    fi: CoreSnapshotInput = snapshot.factor_inputs()[0]
    count = _materialize_panel_leaves(
        _FakeConn(),  # type: ignore[arg-type]
        metrics=PANEL_METRICS,
        factor_input=fi,
        issuer_class=IssuerClass.NON_FINANCIAL,
        gppe_definition=GppeV0Definition(risk_free_rate=Decimal("0.04")),
        financial_payload={"net_income": "250"},
        periods=(date(2025, 12, 31), date(2025, 12, 31), date(2025, 12, 31)),
        freshness=MetricFreshness.FRESH,
        base_confidence=Decimal("0.80"),
        source_evidence=InputEvidenceStatus.VERIFIED,
    )
    assert count >= 7
    by_key = {k: (val, conf) for k, val, conf in inserted}
    assert by_key["revenue"][0] == Decimal("900")
    assert by_key["net_income"][0] == Decimal("250")
    assert by_key["market_cap"] == (Decimal("2000"), Decimal("0.75"))


def test_persist_issuer_profiles_populates_source_fetch_id() -> None:
    from data_engine.datahub.capture_core import issuer_registry
    from factors.snapshot_inputs import OperatingBranch

    iid = "11111111-1111-1111-1111-111111111111"
    executed: list[tuple[str, Any]] = []

    class _Cur:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def fetchone(self) -> Any:
            return self._rows[0] if self._rows else None

        def fetchall(self) -> list[Any]:
            return self._rows

    class _Conn:
        def execute(self, sql: str, params: Any = None) -> _Cur:
            executed.append((sql, params))
            if "to_regclass" in sql:
                return _Cur([("staging.issuer_profiles",)])
            if "from raw.fetches" in sql:
                return _Cur([("0000320193", 98765)])
            return _Cur([])

    written = issuer_registry.persist_issuer_profiles(
        _Conn(),
        classifications={
            320193: issuer_registry.IssuerClassification(
                operating_branch=OperatingBranch.NON_FINANCIAL,
                sic="3571",
                sic_description="Electronic Computers",
                fiscal_year_end="0930",
                primary_exchange="Nasdaq",
                entity_type="operating",
            )
        },
        issuer_ids_by_cik={320193: [iid]},
        knowable_at=datetime(2026, 11, 26, tzinfo=UTC),
    )
    assert written == 1
    inserts = [(sql, params) for sql, params in executed if "insert into staging.issuer_profiles" in sql]
    assert len(inserts) == 1
    assert "source_fetch_id" in inserts[0][0]
    assert inserts[0][1][9] == 98765


def test_known_answer_outcomes_evaluates_operating_branch_and_negative_themes() -> None:
    from data_engine.datahub.oracle_outcomes import known_answer_outcomes

    hban_obs = "normalized-observation:" + ("a" * 64)
    sbux_obs = "normalized-observation:" + ("b" * 64)
    part_id = "segment-partition:" + ("c" * 64)

    class _Cur:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def fetchall(self) -> list[Any]:
            return self._rows

    class _Conn:
        def execute(self, sql: str, _params: Any = None) -> _Cur:
            if "mart.metric_node_values" in sql:
                return _Cur([])
            if "mart.issuer_theme_purity" in sql:
                return _Cur(
                    [
                        (
                            "issuer:cik:0000829224",
                            829224,
                            "semiconductors",
                            Decimal("0"),
                            part_id,
                            date(2025, 9, 28),
                            datetime(2026, 6, 30, tzinfo=UTC),
                        ),
                        (
                            "issuer:cik:0000829224",
                            829224,
                            "cloud-software",
                            Decimal("0.25"),
                            part_id,
                            date(2025, 9, 28),
                            datetime(2026, 6, 30, tzinfo=UTC),
                        ),
                    ]
                )
            if "o.semantic_type = 'financial-fact'" in sql:
                return _Cur(
                    [
                        (
                            "issuer:cik:0000049196",
                            hban_obs,
                            datetime(2026, 2, 20, tzinfo=UTC),
                            {"issuer_id": "issuer:cik:0000049196", "operating_branch": "financial"},
                        ),
                        (
                            "issuer:cik:0000829224",
                            sbux_obs,
                            datetime(2026, 2, 20, tzinfo=UTC),
                            {"issuer_id": "issuer:cik:0000829224", "operating_branch": "non_financial"},
                        ),
                    ]
                )
            if "p.normalized_payload ->> 'ticker'" in sql:
                return _Cur([("HBAN", "issuer:cik:0000049196"), ("SBUX", "issuer:cik:0000829224")])
            return _Cur([])

    outcomes = known_answer_outcomes(
        _Conn(),  # type: ignore[arg-type]
        "capture-run:" + ("1" * 64),
        datetime(2026, 6, 30, tzinfo=UTC),
        [],
    )
    by_field = {(o.subject_id, o.field): o for o in outcomes}
    hban_branch = by_field[("issuer:cik:0000049196", "operating_branch")]
    assert hban_branch.result == "agree"
    assert by_field[("issuer:cik:0000829224", "negative_theme:semiconductors")].result == "agree"
    assert by_field[("issuer:cik:0000829224", "negative_theme:cloud-software")].result == "disagree"
