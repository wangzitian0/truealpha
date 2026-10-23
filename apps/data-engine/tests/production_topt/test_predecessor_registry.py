"""#496: the issuer CIK predecessor registry (migration 0037) and its
resolution precedence in planning. Skips without a local Postgres."""

import os

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.composition import predecessor_ciks

_XOM_ISSUER = "issuer:lei:J3WHBG0MTS7O8ZVMDC91"
_XOM_LISTING = "listing:xnys:xom"


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        yield active
    finally:
        active.rollback()
        active.close()


def test_seeded_registry_row_resolves_xom(connection) -> None:
    resolved = predecessor_ciks(connection, [_XOM_LISTING], {_XOM_LISTING: _XOM_ISSUER})
    assert resolved.get(_XOM_LISTING) == 34088, "the owner-signed 0037 seed must resolve XOM"


def test_registry_rows_are_append_only_and_signed(connection) -> None:
    reason, approver = connection.execute(
        "select reason, approved_by from staging.issuer_cik_predecessors where issuer_id = %s",
        (_XOM_ISSUER,),
    ).fetchone()
    assert "2115436" in reason and "zitian" in approver
    with pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "update staging.issuer_cik_predecessors set predecessor_cik = 1 where issuer_id = %s",
            (_XOM_ISSUER,),
        )


def test_unregistered_issuer_resolves_nothing(connection) -> None:
    resolved = predecessor_ciks(
        connection, ["listing:xnas:aapl"], {"listing:xnas:aapl": "issuer:lei:HWUPKR0MPOU8FGXBT394"}
    )
    assert "listing:xnas:aapl" not in resolved, "no registry row and no lineage -> honest absence"


def test_canonical_uuid_resolves_signed_predecessor_via_entity_aliases(connection, monkeypatch) -> None:
    """#1263: canonical_universe hands `predecessor_ciks` and `build_route` canonical entity UUIDs,
    while `staging.issuer_cik_predecessors` keys ExxonMobil by `issuer:lei:J3WHBG0MTS7O8ZVMDC91`."""
    from datetime import UTC, date, datetime

    from data_engine.datahub.production_topt import issuer_registry
    from data_engine.datahub.production_topt.sec_financial_adapter import OperatingBranch, build_route
    from data_engine.datahub.production_topt.source_registrations import RouteCell, RouteContext
    from data_engine.datahub.resolve_coordinates import resolve_entity

    monkeypatch.setattr(
        issuer_registry,
        "resolve_issuer_classifications",
        lambda ciks: issuer_registry.RegistryReading(
            classifications={
                cik: issuer_registry.IssuerClassification(
                    operating_branch=OperatingBranch.NON_FINANCIAL,
                    revenue_proxy_allowed=False,
                )
                for cik in ciks.values()
            }
        ),
    )

    known_at = datetime(2024, 1, 1, tzinfo=UTC)
    xom_uuid = str(resolve_entity(connection, _XOM_ISSUER, "issuer", as_of=known_at.date(), known_at=known_at))
    xom_instrument_uuid = str(
        resolve_entity(
            connection, "instrument:equity:xom:common", "instrument", as_of=known_at.date(), known_at=known_at
        )
    )
    xom_listing_uuid = str(
        resolve_entity(connection, _XOM_LISTING, "listing", as_of=known_at.date(), known_at=known_at)
    )
    connection.execute(
        """
        insert into staging.entity_aliases (
            entity_id, scheme, value, valid_from, valid_to, transaction_time, source,
            raw_ref, method, confidence, mapping_version
        ) values (%s::uuid, 'cik', '0002115436', '-infinity'::date, null, least(%s, clock_timestamp()), 'datahub.capture', %s, 'asserted', 1.0, 'datahub:v1')
        on conflict do nothing
        """,
        (xom_uuid, known_at, f"mint:{xom_uuid}:cik"),
    )

    resolved = predecessor_ciks(connection, [xom_listing_uuid], {xom_listing_uuid: xom_uuid})
    assert resolved.get(xom_listing_uuid) == 34088
    assert resolved[xom_listing_uuid].signed is True

    cutoff = datetime(2026, 9, 2, tzinfo=UTC)
    context = RouteContext(
        cutoff=cutoff,
        cutoff_date=date(2026, 9, 2),
        price_cutoff_date=date(2026, 9, 2),
        partition_start=datetime(2026, 9, 1, tzinfo=UTC),
        universe_published_at=None,
        coordinates={
            xom_listing_uuid: (xom_uuid, xom_instrument_uuid, xom_listing_uuid, "XOM"),
        },
        connection=connection,
    )
    from data_engine.datahub.production_topt.executor import FetchSuccess
    from data_engine.sources import sec
    from truealpha_contracts.datahub import CaptureWorkItem

    work_item = CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + "a" * 64,
        schedule_policy_id="schedule-policy:" + "2" * 64,
    )
    cell = RouteCell(
        work_item_id=work_item.work_item_id,
        semantic_type="financial-fact",
        issuer_id=xom_uuid,
        instrument_id=xom_instrument_uuid,
        listing_id=xom_listing_uuid,
        ticker="XOM",
    )

    adapter = build_route(context, [cell])
    target = adapter._targets[cell.work_item_id]
    assert target.cik == 2115436
    assert target.predecessor_cik == 34088
    assert target.predecessor_signed is True
    assert target.revenue_proxy_allowed is False

    holdco_doc = {
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "end": "2026-06-30",
                                "val": 464482000000,
                                "accn": "0002115436-26-000012",
                                "fy": 2026,
                                "fp": "Q2",
                                "form": "10-Q",
                                "filed": "2026-08-03",
                            }
                        ]
                    }
                },
                "CommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "end": "2026-06-30",
                                "val": 4111911960,
                                "accn": "0002115436-26-000012",
                                "fy": 2026,
                                "fp": "Q2",
                                "form": "10-Q",
                                "filed": "2026-08-03",
                            }
                        ]
                    }
                },
            }
        }
    }
    pred_doc = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "val": 332238000000,
                                "accn": "0000034088-26-000017",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-18",
                            }
                        ]
                    }
                },
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "val": 28844000000,
                                "accn": "0000034088-26-000017",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-18",
                            }
                        ]
                    }
                },
            }
        }
    }
    monkeypatch.setattr(
        sec,
        "fetch_company_facts_response",
        lambda cik: (b'{"cik":2115436}', holdco_doc) if cik == 2115436 else (b'{"cik":34088}', pred_doc),
    )
    outcome = adapter.fetch(work_item)
    assert isinstance(outcome, FetchSuccess)
    assert outcome.record.payload["revenue"] == "332238000000"
    assert outcome.record.payload["net_income"] == "28844000000"
    assert outcome.record.payload["total_assets"] == "464482000000"
    assert outcome.record.payload["shares_outstanding"] == "4111911960"
    assert outcome.record.payload["gross_profit"] is None
