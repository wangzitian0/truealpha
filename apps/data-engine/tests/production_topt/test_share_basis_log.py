"""#1606: the log line of a share count that cannot be put on the share basis of its close.

`freeze_snapshot` builds one member per listing. A member that holds a filed share count but no
proven share basis loses its market value, and the loss gets a named cause (`unavailable_reason`).
The log line must tell a real loss from a gap that the environment contract declares:

* A universe whose contract record says `price_series: false` (the canary) stores no daily bars. Its
  listings cannot show a split. The gap is the contract's own consequence: one INFO line for each
  listing and run, with the reason. The cause stays on the member.
* Every other loss stays a WARNING: a universe with a price series, a split derivation that failed,
  a missing listing unit ratio, an issuer without a listing ticker.
* A share count that the source never published is no loss of ours. The member keeps no cause and the
  core names it `missing_shares_outstanding`. The member builder logs nothing for it.

The DB-backed tests skip without a local Postgres; ci-python runs them migrated.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt import PostgresToptCoreRepository
from data_engine.datahub.share_basis import NO_SPLIT_ADJUSTED_SERIES, SPLIT_DERIVATION_FAILED, Unavailable
from factors.production_topt import MetricAvailability, ToptCoreReasonCode

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parents[1] / "support"))
from production_topt.test_materialization import (  # noqa: E402
    _SYNTHETIC_REPORT_DATE,
    _member_rows_for_probe,
    _seed_complete_production_run,
    _synthetic_corpus,
)
from split_series_seed import share_count_vintage  # noqa: E402

MATERIALIZATION_LOGGER = "data_engine.datahub.production_topt.materialization"
VINTAGE = {"shares_outstanding": share_count_vintage(filed=date(2026, 3, 2), own_date=date(2026, 2, 20))}


@pytest.fixture
def connection():
    try:
        active = psycopg.connect(settings.database_url, connect_timeout=3, autocommit=False)
    except psycopg.OperationalError as error:
        if os.environ.get("DATABASE_URL") or os.environ.get("TRUEALPHA_REQUIRE_RUNTIME"):
            pytest.fail(f"configured Postgres is unreachable: {error}", pytrace=False)
        pytest.skip("no local Postgres; CI runs the required integration coverage")
    try:
        active.execute("select 1")
        yield active
    finally:
        active.rollback()
        active.close()


def _share_basis_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == MATERIALIZATION_LOGGER and record.getMessage().startswith("share basis")
    ]


def _lines_naming(records: list[logging.LogRecord], listing_id: str) -> list[str]:
    """The lines that open with the listing id. A prefix match is wrong: `goog` is a prefix of `googl`."""
    return [record.getMessage() for record in records if record.getMessage().startswith(f"share basis: {listing_id} ")]


def _canary_corpus() -> dict:
    """The synthetic 3-listing corpus under a canary universe id. The contract record of the canary has no price series."""
    corpus = _synthetic_corpus()
    corpus["topt_denominator"]["universe_id"] = f"universe:canary-us-{_SYNTHETIC_REPORT_DATE}"
    return corpus


def test_a_universe_without_a_price_series_logs_each_share_gap_once_at_info(connection, caplog) -> None:
    """The canary stores no daily bars, so each of its three listings has no split history. Red on main: three WARNINGs."""
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection, corpus=_canary_corpus(), financial_vintage=VINTAGE
    )
    caplog.set_level(logging.INFO, logger=MATERIALIZATION_LOGGER)

    snapshot = PostgresToptCoreRepository(connection).freeze_snapshot(
        run_id=run.run_id, release_manifest_id=release_manifest_id
    )

    records = _share_basis_records(caplog)
    assert [record.levelno for record in records] == [logging.INFO] * 3
    listing_ids = sorted(member.listing_id for member in snapshot.members)
    assert listing_ids == ["listing:xsyn:alfa", "listing:xsyn:alfb", "listing:xsyn:brav"]
    for listing_id in listing_ids:
        named = _lines_naming(records, listing_id)
        assert len(named) == 1, f"{listing_id} must be named in exactly one line"
        assert "no_split_adjusted_series" in named[0]
        assert "price_series=false" in named[0]
    # The market value stays NULL and the member keeps the cause.
    assert {member.shares_outstanding.value for member in snapshot.members} == {None}
    assert {member.shares_outstanding.unavailable_reason for member in snapshot.members} == {
        ToptCoreReasonCode.NO_SPLIT_ADJUSTED_SERIES
    }


def test_a_universe_with_a_price_series_keeps_one_warning_for_each_listing_and_run(connection, caplog) -> None:
    """The TOPT universe stores daily bars. A listing without bars is a real loss: the WARNING stays, one for each listing.

    A second `freeze_snapshot` of the same run returns the stored snapshot and logs nothing.
    """
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(connection, financial_vintage=VINTAGE)
    caplog.set_level(logging.INFO, logger=MATERIALIZATION_LOGGER)
    repository = PostgresToptCoreRepository(connection)

    snapshot = repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)
    first = _share_basis_records(caplog)
    repository.freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)

    assert len(snapshot.members) == 21
    assert [record.levelno for record in first] == [logging.WARNING] * 21
    for member in snapshot.members:
        assert len(_lines_naming(first, member.listing_id)) == 1, f"{member.listing_id} must be named in one line"
    assert all("no_split_adjusted_series" in record.getMessage() for record in first)
    assert len(_share_basis_records(caplog)) == 21, "a second freeze of the same run must log nothing"


def test_a_universe_id_outside_the_contract_keeps_the_warning(connection, caplog) -> None:
    """No record says that this universe holds no price series, so no declared gap exists. The WARNING stays."""
    (_, run, _, release_manifest_id, *_rest) = _seed_complete_production_run(
        connection, corpus=_synthetic_corpus(), financial_vintage=VINTAGE
    )
    caplog.set_level(logging.INFO, logger=MATERIALIZATION_LOGGER)

    PostgresToptCoreRepository(connection).freeze_snapshot(run_id=run.run_id, release_manifest_id=release_manifest_id)

    assert [record.levelno for record in _share_basis_records(caplog)] == [logging.WARNING] * 3


@pytest.mark.parametrize(
    ("histories", "payload_edit", "reason"),
    [
        pytest.param(
            (Unavailable(SPLIT_DERIVATION_FAILED, "NVDA: the price ratio steps twice"),),
            {},
            "split_derivation_failed",
            id="stored-bars-that-prove-no-split",
        ),
        pytest.param(
            (None, Unavailable(NO_SPLIT_ADJUSTED_SERIES, "NVDQ: unadjusted bars without split-adjusted bars")),
            {},
            "no_split_adjusted_series",
            id="one-listing-with-a-partial-series",
        ),
        pytest.param((None,), {"listing_units_per_share": None}, "missing_listing_unit_ratio", id="no-unit-ratio"),
        pytest.param((), {}, "no_split_adjusted_series", id="issuer-without-a-listing-ticker"),
    ],
)
def test_a_universe_without_a_price_series_keeps_the_warning_for_a_genuine_loss(
    histories, payload_edit, reason, caplog
) -> None:
    """The INFO line covers only a listing that has a ticker and no stored bar. Another loss stays a WARNING."""
    rows = _member_rows_for_probe(financial_vintage=VINTAGE)
    rows["financial-fact"].payload.update(payload_edit)
    caplog.set_level(logging.INFO, logger=MATERIALIZATION_LOGGER)

    member = PostgresToptCoreRepository._snapshot_member(
        "listing:xnas:nvda", rows, histories=histories, stores_price_series=False
    )

    records = _share_basis_records(caplog)
    assert [record.levelno for record in records] == [logging.WARNING]
    assert reason in records[0].getMessage()
    assert member.shares_outstanding.value is None
    assert member.shares_outstanding.unavailable_reason is ToptCoreReasonCode(reason)


def test_a_share_count_that_the_source_never_published_keeps_no_cause_and_logs_nothing(caplog) -> None:
    """The fixture holds no share count for this ticker. The market value stays NULL.

    The member keeps no `unavailable_reason`: the core then names `missing_shares_outstanding`
    (`_share_count_reasons`). The loss is on record in the data and no line of ours says otherwise.
    """
    rows = _member_rows_for_probe()
    rows["financial-fact"].payload["shares_outstanding"] = None
    rows["financial-fact"].payload.pop("shares_period_end", None)
    caplog.set_level(logging.DEBUG, logger=MATERIALIZATION_LOGGER)

    member = PostgresToptCoreRepository._snapshot_member("listing:xnas:nvda", rows, histories=(None,))

    assert member.shares_outstanding.value is None
    assert member.shares_outstanding.availability is MetricAvailability.UNAVAILABLE
    assert member.shares_outstanding.unavailable_reason is None
    assert _share_basis_records(caplog) == []
