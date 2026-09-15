"""Module 6's producer against a real database (#772, init.md §0 question 6).

The chain q6 is: an accepted segment partition -> a model's per-segment verdict -> the
share -> a mart row. Each link has its own tests; this is the join, and it runs against
Postgres because that is where the two things most likely to drift live — the columns the
producer writes and the constraints the plane enforces.

The model is intercepted at the HTTP boundary (`llm._gateway_transport`), not by replacing
`classify_segments`: the ledger row, the invocation record and the replay all run for real,
so what is exercised here is the path production takes rather than a shortcut around it.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from data_engine.config import settings
from data_engine.datahub.production_topt.theme_purity import (
    load_partitions,
    materialize_theme_purity,
    summary_line,
)
from data_engine.sources import gateway, llm
from truealpha_contracts.theme_purity import THEMES

CIK = 999_000_042  # outside the real corpus; these rows are this test's own
ISSUER = f"issuer:cik:{CIK:010d}"
PARTITION = "segment-partition:" + "d" * 64
RUN_ID = "test-run:theme-purity"
CUTOFF = datetime(2026, 9, 1, tzinfo=UTC)
KNOWABLE = datetime(2025, 12, 12, tzinfo=UTC)
AI = THEMES["ai-infrastructure"]

#: The AVGO shape, in absolute dollars as the plane holds them.
SEGMENTS = (("Semiconductor solutions", Decimal("36858000000")), ("Infrastructure software", Decimal("27029000000")))
TOTAL = Decimal("63887000000")


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


@pytest.fixture
def governed(monkeypatch):
    """Make CIK the one member of RUN_ID, under ISSUER.

    These tests are about the share, not about membership, and RUN_ID is not a captured run.
    What membership resolves from a REAL run's capture plane is asserted at the bottom of
    this file, against a run the deployed executor wrote (#828)."""
    from data_engine.datahub.production_topt import theme_purity

    monkeypatch.setattr(theme_purity, "governed_members", lambda _connection, *, run_id: {CIK: ISSUER})


@pytest.fixture
def seated(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "glm-test")
    ledger = gateway.MemoryLedger()
    previous = gateway.set_writer(ledger)
    yield ledger
    gateway.set_writer(previous)


def _seed(connection, *, segments=SEGMENTS, total=TOTAL, residual="0", partition=PARTITION, knowable=KNOWABLE, cik=CIK):
    for name, revenue in segments:
        connection.execute(
            """
            insert into staging.issuer_segment_revenue_facts
                (cik, segment_name, segment_revenue, partition_id, partition_total,
                 partition_residual, knowable_at, period_end, source, evidence_ref,
                 extractor, confidence)
            values (%s, %s, %s, %s, %s, %s, %s, '2025-11-02', '10k-segment-extraction',
                    'accession=0001730168-25-000121 form=10-K', 'rule:exhaustive-partition:v1', 0.85)
            """,
            (cik, name, revenue, partition, total, Decimal(residual), knowable),
        )


def _seed_single_segment(
    connection, *, extractor, evidence, partition="segment-partition:" + "a" * 64, knowable=KNOWABLE
):
    connection.execute(
        """
        insert into staging.issuer_segment_revenue_facts
            (cik, segment_name, segment_revenue, partition_id, partition_total,
             partition_residual, knowable_at, period_end, source, evidence_ref,
             extractor, confidence)
        values (%s, 'Single reportable segment', %s, %s, %s, 0, %s, '2025-12-31',
                '10k-segment-extraction', %s, %s, 0.75)
        """,
        (CIK, TOTAL, partition, TOTAL, knowable, evidence, extractor),
    )


def _answers(*verdicts):
    """One canned classifier reply per model call, in order."""
    replies = [
        json.dumps(
            {
                "model": "glm-served",
                "choices": [{"message": {"content": json.dumps({"verdicts": list(v)})}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 60, "total_tokens": 260},
            }
        ).encode()
        for v in verdicts
    ]

    def transport(url, headers, body):  # noqa: ARG001
        transport.sent.append(json.loads(body))
        return 200, replies[min(len(transport.sent) - 1, len(replies) - 1)]

    transport.sent = []
    return transport


def _rows(connection):
    return connection.execute(
        """
        select theme_id, theme_share, consolidated_revenue, in_theme_revenue, out_of_theme_revenue,
               unclassified_revenue, segments, confidence, reason_codes, extractor,
               availability_status, source_evidence_status, partition_id, period_end
        from mart.issuer_theme_purity where run_id = %s order by theme_id
        """,
        (RUN_ID,),
    ).fetchall()


def test_the_partition_is_loaded_as_one_set_per_issuer(connection) -> None:
    _seed(connection)
    partitions = load_partitions(connection, cutoff=CUTOFF)
    mine = [p for p in partitions if p.cik == CIK]
    assert len(mine) == 1, "one issuer, one accepted set — not one per row"
    assert mine[0].consolidated_revenue == TOTAL
    assert dict(mine[0].parts) == dict(SEGMENTS)
    assert mine[0].accession == "0001730168-25-000121", "the model's replay coordinate is the FILING"
    assert mine[0].issuer_id == ISSUER


def test_a_partition_filed_after_the_cutoff_is_not_visible(connection) -> None:
    """PIT. A filing that landed after the run's cutoff must not change a row attributed to
    that run — the look-ahead `fund_consolidation` records for N-PORT weights, in this
    plane's shape."""
    _seed(connection, knowable=datetime(2026, 12, 1, tzinfo=UTC))
    assert [p for p in load_partitions(connection, cutoff=CUTOFF) if p.cik == CIK] == []


def test_a_published_share_is_over_the_consolidated_total(connection, seated, governed, monkeypatch) -> None:
    """The whole point of module 6, asserted through the deployed writer: 36,858 / 63,887,
    not 36,858 / (what the classifier happened to judge)."""
    _seed(connection)
    monkeypatch.setattr(
        llm,
        "_gateway_transport",
        _answers(
            [
                {"index": 0, "in_theme": True, "reason": "accelerators"},
                {"index": 1, "in_theme": False, "reason": "enterprise software"},
            ]
        ),
    )
    written = materialize_theme_purity(connection, run_id=RUN_ID, cutoff=CUTOFF, themes=(AI,))
    mine = [row for row in written if row.entity_id == ISSUER]
    assert len(mine) == 1

    rows = [r for r in _rows(connection) if r[0] == AI.theme_id]
    assert len(rows) == 1
    (
        _theme,
        share,
        total,
        in_theme,
        out_of_theme,
        unclassified,
        segments,
        confidence,
        reasons,
        extractor,
        avail,
        evidence,
        partition_id,
        period_end,
    ) = rows[0]
    assert share == Decimal("36858000000") / TOTAL
    assert total == TOTAL and in_theme == Decimal("36858000000") and out_of_theme == Decimal("27029000000")
    assert unclassified == 0 and segments == 2
    assert reasons == [] and avail == "available" and evidence == "verified"
    assert extractor.startswith("model:glm-served:"), "the row names who judged it"
    assert partition_id == PARTITION, "and which set it was computed over"
    assert period_end.isoformat() == "2025-11-02"
    assert confidence == Decimal("0.85")


def test_a_mostly_unclassified_issuer_is_refused_rather_than_ranked(connection, seated, governed, monkeypatch) -> None:
    """The definition's floor. A share of 0.58 computed while the classifier declined on 42%
    of the issuer's revenue is not a purity — and publishing it would rank an issuer the
    model could not read against issuers it could."""
    _seed(connection)
    monkeypatch.setattr(
        llm,
        "_gateway_transport",
        _answers([{"index": 0, "in_theme": True, "reason": "accelerators"}]),  # segment 1 unanswered
    )
    materialize_theme_purity(connection, run_id=RUN_ID, cutoff=CUTOFF, themes=(AI,))
    rows = [r for r in _rows(connection) if r[0] == AI.theme_id]
    (_theme, share, _total, _in, _out, unclassified, _segments, confidence, reasons, _ex, avail, evidence, *_) = rows[0]
    assert share is None, "refused, not a small number"
    assert unclassified == Decimal("27029000000"), "and the mass that was never judged is on the row"
    assert "below_minimum_classified_share" in reasons
    assert confidence == 0 and avail == "unavailable" and evidence == "degraded"


def test_the_classifier_is_asked_once_per_theme_and_never_shown_the_revenue(
    connection, seated, governed, monkeypatch
) -> None:
    themes = (THEMES["ai-infrastructure"], THEMES["semiconductors"])
    _seed(connection)
    transport = _answers(
        [{"index": 0, "in_theme": True, "reason": "a"}, {"index": 1, "in_theme": False, "reason": "b"}],
        [{"index": 0, "in_theme": True, "reason": "a"}, {"index": 1, "in_theme": False, "reason": "b"}],
    )
    monkeypatch.setattr(llm, "_gateway_transport", transport)
    materialize_theme_purity(connection, run_id=RUN_ID, cutoff=CUTOFF, themes=themes)

    assert len(transport.sent) == 2, "one ask per theme"
    sent = json.dumps(transport.sent)
    assert "36858000000" not in sent and "27029000000" not in sent
    assert {r[0] for r in _rows(connection)} == {t.theme_id for t in themes}


def test_a_second_run_of_the_same_cutoff_replays_the_model(connection, seated, governed, monkeypatch) -> None:
    """§9: replay never silently calls the model again. This is also what makes a weekly
    recompute cost nothing — the same filings under the same themes are already answered."""
    _seed(connection)
    transport = _answers(
        [{"index": 0, "in_theme": True, "reason": "a"}, {"index": 1, "in_theme": False, "reason": "b"}]
    )
    monkeypatch.setattr(llm, "_gateway_transport", transport)
    materialize_theme_purity(connection, run_id=RUN_ID, cutoff=CUTOFF, themes=(AI,))
    assert len(transport.sent) == 1

    materialize_theme_purity(connection, run_id=RUN_ID, cutoff=CUTOFF, themes=(AI,))
    assert len(transport.sent) == 1, "the second run asked the provider nothing"
    assert len(_rows(connection)) == 1, "and replaced its own row rather than accumulating"


def test_the_row_carries_the_definition_it_was_computed_under(connection, seated, governed, monkeypatch) -> None:
    """Two runs are comparable only under the same sha: the inclusion wording IS the question
    asked, so a reworded theme is a different measurement wearing the same name."""
    _seed(connection)
    monkeypatch.setattr(
        llm,
        "_gateway_transport",
        _answers([{"index": 0, "in_theme": True, "reason": "a"}, {"index": 1, "in_theme": False, "reason": "b"}]),
    )
    materialize_theme_purity(connection, run_id=RUN_ID, cutoff=CUTOFF, themes=(AI,))
    version, sha = connection.execute(
        "select definition_version, definition_sha256 from mart.issuer_theme_purity where run_id = %s limit 1",
        (RUN_ID,),
    ).fetchone()
    assert (version, sha) == (AI.factor_version, AI.content_sha256)


def test_the_summary_names_what_was_published(connection, seated, governed, monkeypatch) -> None:
    _seed(connection)
    monkeypatch.setattr(
        llm,
        "_gateway_transport",
        _answers([{"index": 0, "in_theme": True, "reason": "a"}, {"index": 1, "in_theme": False, "reason": "b"}]),
    )
    written = materialize_theme_purity(connection, run_id=RUN_ID, cutoff=CUTOFF, themes=(AI,))
    line = summary_line(written)
    assert "published" in line and "0.57" in line
    assert summary_line(()) == "theme purity: no issuer has a segment partition at this cutoff"


def test_a_partition_that_rounds_still_lands(connection, seated, governed, monkeypatch) -> None:
    """The defect this pins: the plane's accounting check required the three masses to equal
    the consolidated total on their own. They are computed over the partition's PARTS, and
    the parts sum to `total - partition_residual` — so a filing that rounds (most of them)
    would have had a correct row REFUSED by the database, with the failure surfacing as a
    constraint violation in a weekly job rather than as anything a reader could act on
    (review on #807).

    3,000,000 short on 63,887,000,000: well inside the extraction's tolerance, and the share
    is unchanged because the denominator was never the parts.
    """
    residual = Decimal("3000000")
    short = (("Semiconductor solutions", Decimal("36858000000")), ("Infrastructure software", Decimal("27026000000")))
    _seed(connection, segments=short, residual=str(residual), partition="segment-partition:" + "e" * 64)
    monkeypatch.setattr(
        llm,
        "_gateway_transport",
        _answers(
            [
                {"index": 0, "in_theme": True, "reason": "accelerators"},
                {"index": 1, "in_theme": False, "reason": "enterprise software"},
            ]
        ),
    )
    materialize_theme_purity(connection, run_id=RUN_ID, cutoff=CUTOFF, themes=(AI,))
    rows = connection.execute(
        """
        select theme_share, consolidated_revenue, in_theme_revenue, out_of_theme_revenue,
               unclassified_revenue, partition_residual
        from mart.issuer_theme_purity where run_id = %s and cik = %s
        """,
        (RUN_ID, CIK),
    ).fetchall()
    assert len(rows) == 1, "the row landed rather than being refused by the check"
    share, total, in_theme, out_of_theme, unclassified, stored_residual = rows[0]
    assert in_theme + out_of_theme + unclassified + stored_residual == total, "the identity the check enforces"
    assert in_theme + out_of_theme + unclassified != total, "and it is NOT the identity without the residual"
    assert share == Decimal("36858000000") / TOTAL, "the share is over the total, so rounding does not move it"


def test_a_rerun_refreshes_provenance_not_just_the_numbers(connection, seated, governed, monkeypatch) -> None:
    """A re-run that sees a newer partition at the same cutoff must not leave a share from
    one extraction beside the partition_id of another — a row that reads as re-checkable and
    is not."""
    _seed(connection)
    monkeypatch.setattr(
        llm,
        "_gateway_transport",
        _answers(
            [
                {"index": 0, "in_theme": True, "reason": "a"},
                {"index": 1, "in_theme": False, "reason": "b"},
            ]
        ),
    )
    materialize_theme_purity(connection, run_id=RUN_ID, cutoff=CUTOFF, themes=(AI,))

    later = "segment-partition:" + "f" * 64
    _seed(
        connection,
        segments=(
            ("Semiconductor solutions", Decimal("40000000000")),
            ("Infrastructure software", Decimal("23887000000")),
        ),
        partition=later,
        knowable=datetime(2026, 1, 15, tzinfo=UTC),
    )
    materialize_theme_purity(connection, run_id=RUN_ID, cutoff=CUTOFF, themes=(AI,))

    partition_id, in_theme = connection.execute(
        "select partition_id, in_theme_revenue from mart.issuer_theme_purity where run_id = %s and cik = %s",
        (RUN_ID, CIK),
    ).fetchone()
    assert partition_id == later, "the row names the partition its numbers came from"
    assert in_theme == Decimal("40000000000"), "and the numbers are that partition's"


def test_a_single_segment_issuer_is_described_to_the_classifier_by_its_filing(
    connection, seated, governed, monkeypatch
) -> None:
    """Without this, the single-segment path lands rows and answers nothing.

    Its one part is labelled `Single reportable segment`. No classifier can judge that against
    any theme, so every pure-play would decline, fall below the coverage floor, and be
    refused — the exact outcome the single-segment path exists to prevent. The filing's own
    sentence is on the row because it says what the company DOES, and that is what the model
    is shown.
    """
    statement = (
        "Segment Information The Company has a single operating and reportable segment, "
        "providing an observability and security platform for cloud applications"
    )
    from factors.shared.extraction import RULE_SINGLE_SEGMENT

    _seed_single_segment(
        connection,
        extractor=RULE_SINGLE_SEGMENT,
        evidence=(
            "accession=0001628280-26-008819 form=10-K "
            f"segment_count=us-gaap:NumberOfReportableSegments=1@2025-12-31 single_segment_statement={statement}"
        ),
    )
    transport = _answers([{"index": 0, "in_theme": True, "reason": "observability for cloud"}])
    monkeypatch.setattr(llm, "_gateway_transport", transport)
    materialize_theme_purity(connection, run_id=RUN_ID, cutoff=CUTOFF, themes=(AI,), tickers={ISSUER: "DDOG"})

    asked = json.dumps(transport.sent)
    assert "observability and security platform" in asked, "the model is told what the company does"
    assert '"[0] Single operating segment"' not in asked, "not the unjudgeable label"
    assert f"Issuer: DDOG ({ISSUER})." in asked, "and which issuer it is judging (#849)"

    theme_share, segment_name = connection.execute(
        """
        select p.theme_share, f.segment_name
        from mart.issuer_theme_purity p
        join staging.issuer_segment_revenue_facts f on f.partition_id = p.partition_id
        where p.run_id = %s and p.cik = %s limit 1
        """,
        (RUN_ID, CIK),
    ).fetchone()
    assert theme_share == Decimal(1), "a pure-play judged in the theme is 100% of it"
    assert segment_name == "Single reportable segment", "the plane keeps the row's own label"


def test_a_withdrawn_rules_partition_is_not_read_even_when_it_is_the_newest(connection) -> None:
    """#822. `rule:single-segment:v1` landed Berkshire Hathaway as one segment, and the plane is
    append-only, so the row cannot be deleted. It is withdrawn instead — and the reader has to
    act as if it were never written.

    Two properties, both about WHERE the filter sits. The withdrawn partition here is newer
    than an admissible one, so a reader that picked the newest vintage first and filtered
    second would return nothing for an issuer that has a perfectly good partition.
    """
    _seed(connection)  # the admissible exhaustive partition, knowable 2025-12-12
    _seed_single_segment(
        connection,
        extractor="rule:single-segment:v1",
        evidence="accession=0001067983-26-000001 form=10-K single_segment_statement=significant for one operating segment",
        partition="segment-partition:" + "e" * 64,
        knowable=datetime(2026, 3, 2, tzinfo=UTC),
    )
    mine = [p for p in load_partitions(connection, cutoff=CUTOFF) if p.cik == CIK]
    assert len(mine) == 1
    assert mine[0].partition_id == PARTITION, "the older admissible partition answers, not the newer withdrawn one"
    assert dict(mine[0].parts) == dict(SEGMENTS)


def test_an_issuer_whose_only_partition_was_withdrawn_has_none(connection) -> None:
    _seed_single_segment(
        connection,
        extractor="rule:single-segment:v1",
        evidence="accession=0001067983-26-000001 form=10-K single_segment_statement=significant for one operating segment",
    )
    assert [p for p in load_partitions(connection, cutoff=CUTOFF) if p.cik == CIK] == []


def test_rows_are_the_governed_runs_members_under_the_ids_the_run_gives_them(connection, seated, monkeypatch) -> None:
    """#828, against a run the deployed executor wrote — not a stub of one.

    The TOPT corpus is LEI-keyed, and the producer wrote every partition in the plane under
    `issuer:cik:…`. So the coverage report joined twenty `issuer:lei:…` subjects to rows it
    could never match (q6 on topt: `no_row` 20), and a partition for an issuer OUTSIDE the
    universe was ranked under the run's id as if it were in it. Membership comes from the
    run's own capture plane: the CIK each member's financials were fetched under.
    """
    from pathlib import Path

    from data_engine.datahub.production_topt import PostgresToptCoreRepository
    from data_engine.datahub.production_topt.theme_purity import governed_members
    from data_engine.datahub.question_coverage import gppe_cells, theme_purity_cells
    from factors.production_topt import GppeV0Definition

    # Undone after the test, unlike a bare sys.path.insert (review on #829).
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from production_topt.test_persistence import CUTOFF as CAPTURE_CUTOFF  # noqa: E402
    from production_topt.test_persistence import _capture  # noqa: E402

    plan = _capture(connection, version="test-828-governed-members")
    core = PostgresToptCoreRepository(connection)
    snapshot = core.freeze_snapshot(run_id=plan.run_id, release_manifest_id=plan.release_manifest_id)
    core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))

    members = governed_members(connection, run_id=plan.run_id)
    run_issuers = {cell.subject_id for cell in gppe_cells(connection, plan.run_id)}
    assert run_issuers and all(issuer.startswith("issuer:lei:") for issuer in run_issuers), "the case #828 is about"
    assert set(members.values()) == run_issuers, "every member resolves, to the id the run itself uses"

    member_cik, member_id = min(members.items())
    outsider_cik = CIK
    assert outsider_cik not in members
    knowable = datetime(2026, 2, 1, tzinfo=UTC)
    _seed(connection, cik=member_cik, partition="segment-partition:" + "c" * 64, knowable=knowable)
    _seed(connection, cik=outsider_cik, partition="segment-partition:" + "9" * 64, knowable=knowable)
    monkeypatch.setattr(
        llm,
        "_gateway_transport",
        _answers(
            [{"index": 0, "in_theme": True, "reason": "a"}, {"index": 1, "in_theme": False, "reason": "b"}],
        ),
    )

    materialize_theme_purity(connection, run_id=plan.run_id, cutoff=CAPTURE_CUTOFF, themes=(AI,))

    rows = connection.execute(
        "select issuer_id, cik from mart.issuer_theme_purity where run_id = %s order by issuer_id", (plan.run_id,)
    ).fetchall()
    assert rows == [(member_id, member_cik)], "one member, under the run's own id; the outsider is not ranked"
    assert {cell.subject_id for cell in theme_purity_cells(connection, plan.run_id)} == {member_id}, (
        "and the coverage report's join now finds it"
    )


def test_a_run_that_fetched_unchanged_bytes_still_resolves_every_member(connection, monkeypatch) -> None:
    """#839: the first staging tick after #829 whose SEC bytes had not changed resolved ZERO
    members, and q6 fell from 12/20 to 0/20. Identical bytes write no new observations — they
    stay on the obligation that first produced them — so a second run's own obligations own
    none. What the run's GPPE rows consumed still names every member's CIK."""
    from pathlib import Path

    from data_engine.datahub.production_topt import PostgresToptCoreRepository
    from data_engine.datahub.production_topt.theme_purity import governed_members
    from data_engine.datahub.question_coverage import gppe_cells
    from factors.production_topt import GppeV0Definition

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    import production_topt.test_persistence as persistence  # noqa: E402

    core = PostgresToptCoreRepository(connection)
    # Two daily ticks of ONE universe partition: same version, the next day's cutoff. The
    # financial facts are the same bytes both days, which is exactly what staging fetched.
    first = persistence._capture(connection, version="test-839")
    monkeypatch.setattr(persistence, "CUTOFF", persistence.CUTOFF + timedelta(days=1))
    second = persistence._capture(connection, version="test-839")
    assert second.run_id != first.run_id
    own = connection.execute(
        "select count(*) from staging.capture_normalized_observations n "
        "join raw.capture_obligations o on o.obligation_id = n.capture_obligation_id "
        "where o.run_id = %s and n.semantic_type = 'financial-fact'",
        (second.run_id,),
    ).fetchone()[0]
    assert own == 0, "the second run wrote no financial-fact observation of its own — the shape staging hit"

    snapshot = core.freeze_snapshot(run_id=second.run_id, release_manifest_id=second.release_manifest_id)
    core.materialize(snapshot, gppe_definition=GppeV0Definition(risk_free_rate="0.05"))
    members = governed_members(connection, run_id=second.run_id)
    assert set(members.values()) == {cell.subject_id for cell in gppe_cells(connection, second.run_id)}
    assert len(set(members.values())) == 20
