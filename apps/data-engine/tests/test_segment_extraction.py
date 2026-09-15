"""The filer's own statements of its segments, read from packaged real filings (#772, q6).

Driven by real 10-Ks rather than constructed strings, because the rules below each came from a
filing that broke a simpler one: a sentence about "one operating segment" that was about
something else (#822), a count tagged only in the hidden header, two concepts nested around one
printed word, segment revenue tagged under two concepts that disagree (#830).

The filings are PACKAGED, and their absence is a failure rather than a skip. An earlier version
of this file pointed at a gitignored directory, so every assertion silently skipped in CI while
the file read as proof (review on #805). A test that cannot run is not a weaker test; it is a
green check standing where a check was supposed to be.

The printed-table reader these tests used to exercise is retired (#833); what it was measured to
do is recorded on that issue.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from data_engine.datahub.standards.filing_extraction import filing_plain_text
from data_engine.datahub.standards.segment_extraction import (
    declared_segment_count,
    segment_name_for,
    single_segment_statement,
    tagged_segment_revenues,
)

FILINGS = Path(__file__).resolve().parents[1] / "samples" / "filings"


def test_the_filers_tagged_count_decides_one_segment_and_a_sentence_does_not() -> None:
    """A pure-play IS the purest name under its theme, so a single-segment issuer is a
    determinate answer — and because its one-part partition cannot fail the identity, what
    decides it has to be the strongest statement the filing makes (#822).

    A sentence was not that. Over the 106 filings the deployed lane walks, "one/single
    operating/reportable segment" matched 48 issuers and at least five of them report several;
    Berkshire Hathaway was landed as one segment. The packaged corpus holds both halves of why
    the inline-XBRL count replaced it: every filing here that reports several segments tags how
    many, and PLUG writes the sentence while tagging nothing — so the sentence alone now
    decides nothing.
    """
    root = FILINGS
    measured = {}
    for path in sorted(root.glob("*.html")):
        if "8K" in path.name:
            continue
        declared = declared_segment_count(path.read_bytes())
        measured[path.name.removesuffix(".html")] = None if declared is None else declared.value
    assert measured == {
        "AAPL_10K_000032019325000079": None,
        "ADM_10K_000000708426000011": 3,
        "ADP_10K_000000867026000030": 2,
        "AVGO_10K_000173016825000121": 2,
        "DDOG_10K_000162828026008819": 1,
        "DUOL_10K_000162828026012494": 1,
        "JPM_10K_000162828026008131": 3,
        "NICE_20F_000100393526000010": 2,
        "PLUG_10KA_000155837022003577": None,
        "PLUG_10K_000155837021007147": None,
        "SHOP_10K_000159480526000007": 1,
    }
    plug = filing_plain_text((root / "PLUG_10K_000155837021007147.html").read_bytes())
    assert single_segment_statement(plug) is not None, "PLUG's prose does say one segment"


def test_the_statement_is_the_sentence_the_count_is_tagged_in() -> None:
    """The row's description is the filer's own tagged sentence, not the first sentence that
    mentions a segment. SHOP tags the word with BOTH concepts, one tag nested in the other, and
    both must read "one". DDOG tags its count only in the hidden header, where there is no
    printed sentence to read back."""
    root = FILINGS
    shop = declared_segment_count((root / "SHOP_10K_000159480526000007.html").read_bytes())
    assert shop is not None and shop.concept == "us-gaap:NumberOfReportableSegments"
    assert shop.statement is not None
    assert "the Company operates in one single operating and reportable segment" in shop.statement
    assert "segment-count" not in shop.statement, "the mark that located the tag is not part of the sentence"

    ddog = declared_segment_count((root / "DDOG_10K_000162828026008819.html").read_bytes())
    assert ddog is not None and ddog.value == 1
    assert ddog.statement is None, "a hidden fact has no printed sentence"


def test_tagged_segment_revenue_reads_as_the_filer_tagged_it() -> None:
    """What the tags yield on the packaged filings, per concept, before any identity is applied.

    AAPL and AVGO tag exactly their consolidated revenue. ADM tags its segments under TWO
    concepts that measure different things — contract revenue and total revenue — which is why
    each concept is its own set and never merged with the other. A single-segment filer (DDOG)
    tags no segment revenue at all; its declaration answers instead.
    """

    def sets(name: str) -> dict[str, tuple[int, Decimal, str | None]]:
        return {
            tagged.concept.split(":", 1)[1]: (
                len(tagged.parts),
                sum((v for _, v in tagged.parts), Decimal(0)),
                tagged.refusal,
            )
            for tagged in tagged_segment_revenues((FILINGS / name).read_bytes())
        }

    assert sets("AAPL_10K_000032019325000079.html") == {
        "RevenueFromContractWithCustomerExcludingAssessedTax": (5, Decimal("416161000000"), None)
    }
    assert sets("AVGO_10K_000173016825000121.html") == {
        "RevenueFromContractWithCustomerExcludingAssessedTax": (2, Decimal("63887000000"), None)
    }
    assert sets("ADM_10K_000000708426000011.html") == {
        "RevenueFromContractWithCustomerExcludingAssessedTax": (3, Decimal("24507000000"), None),
        "Revenues": (3, Decimal("79820000000"), None),
    }
    assert sets("JPM_10K_000162828026008131.html") == {
        "RevenuesNetOfInterestExpense": (3, Decimal("178556000000"), None)
    }
    assert sets("DDOG_10K_000162828026008819.html") == {}


def test_a_segment_is_named_in_its_members_own_words() -> None:
    assert segment_name_for("aapl:RestOfAsiaPacificSegmentMember") == "Rest of Asia Pacific"
    assert segment_name_for("msft:ProductivityAndBusinessProcessesMember") == "Productivity and Business Processes"
    assert segment_name_for("amzn:AWSSegmentMember") == "AWS"
    assert segment_name_for("avgo:SemiconductorSolutionsMember") == "Semiconductor Solutions"
