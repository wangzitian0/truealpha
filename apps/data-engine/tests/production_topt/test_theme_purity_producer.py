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
from datetime import UTC, datetime
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
def seated(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "glm-test")
    ledger = gateway.MemoryLedger()
    previous = gateway.set_writer(ledger)
    yield ledger
    gateway.set_writer(previous)


def _seed(connection, *, segments=SEGMENTS, total=TOTAL, residual="0", partition=PARTITION, knowable=KNOWABLE):
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
            (CIK, name, revenue, partition, total, Decimal(residual), knowable),
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


def test_a_published_share_is_over_the_consolidated_total(connection, seated, monkeypatch) -> None:
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


def test_a_mostly_unclassified_issuer_is_refused_rather_than_ranked(connection, seated, monkeypatch) -> None:
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


def test_the_classifier_is_asked_once_per_theme_and_never_shown_the_revenue(connection, seated, monkeypatch) -> None:
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


def test_a_second_run_of_the_same_cutoff_replays_the_model(connection, seated, monkeypatch) -> None:
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


def test_the_row_carries_the_definition_it_was_computed_under(connection, seated, monkeypatch) -> None:
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


def test_the_summary_names_what_was_published(connection, seated, monkeypatch) -> None:
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


def test_a_partition_that_rounds_still_lands(connection, seated, monkeypatch) -> None:
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


def test_a_rerun_refreshes_provenance_not_just_the_numbers(connection, seated, monkeypatch) -> None:
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


def test_a_single_segment_issuer_is_described_to_the_classifier_by_its_filing(connection, seated, monkeypatch) -> None:
    """Without this, the single-segment path lands rows and answers nothing.

    Its one part is labelled `Single operating segment`. No classifier can judge that against
    any theme, so every pure-play would decline, fall below the coverage floor, and be
    refused — the exact outcome the single-segment path exists to prevent. The filing's own
    sentence is on the row because it says what the company DOES, and that is what the model
    is shown.
    """
    statement = (
        "Segment Information The Company has a single operating and reportable segment, "
        "providing an observability and security platform for cloud applications"
    )
    connection.execute(
        """
        insert into staging.issuer_segment_revenue_facts
            (cik, segment_name, segment_revenue, partition_id, partition_total,
             partition_residual, knowable_at, period_end, source, evidence_ref,
             extractor, confidence)
        values (%s, 'Single operating segment', %s, %s, %s, 0, %s, '2025-12-31',
                '10k-segment-extraction', %s, 'rule:single-segment:v1', 0.75)
        """,
        (
            CIK,
            TOTAL,
            "segment-partition:" + "a" * 64,
            TOTAL,
            KNOWABLE,
            f"accession=0001628280-26-008819 form=10-K single_segment_statement={statement}",
        ),
    )
    transport = _answers([{"index": 0, "in_theme": True, "reason": "observability for cloud"}])
    monkeypatch.setattr(llm, "_gateway_transport", transport)
    materialize_theme_purity(connection, run_id=RUN_ID, cutoff=CUTOFF, themes=(AI,))

    asked = json.dumps(transport.sent)
    assert "observability and security platform" in asked, "the model is told what the company does"
    assert '"[0] Single operating segment"' not in asked, "not the unjudgeable label"

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
    assert segment_name == "Single operating segment", "the plane keeps the row's own label"
