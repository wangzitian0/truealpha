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

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from data_engine.contracts.standards import confidence_for
from data_engine.datahub.standards.filing_extraction import filing_plain_text
from data_engine.datahub.standards.segment_extraction import (
    TaggedPartition,
    TaggedSegments,
    _eliminations,
    _is_geographic_member,
    _is_sum_of_others,
    _without_subtotals,
    accepted_tagged_partition,
    declared_segment_count,
    segment_name_for,
    single_segment_statement,
    tagged_segment_revenues,
)
from factors.shared.extraction import (
    RULE_ELIMINATION_PARTITION,
    RULE_EXHAUSTIVE_PARTITION,
    RULE_SUBTOTAL_FREE_ELIMINATION_PARTITION,
    RULE_SUBTOTAL_FREE_PARTITION,
)

FILINGS = Path(__file__).resolve().parents[1] / "samples" / "filings"
CONSOLIDATION_AXIS = "srt:ConsolidationItemsAxis"


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
        "V_10K_000140316125000089": 1,
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


def test_the_segment_note_is_read_through_its_continuation_chain() -> None:
    """The note's `ix:nonNumeric` holds its heading; the body follows in the `ix:continuation`
    elements the tag names (#841). SHOP: the heading, then two parts; AVGO: 20 bytes, then five.
    DDOG tags no note at all. Visa's note is what its row is described by, in place of the
    sentence its count is tagged in."""
    from data_engine.datahub.standards.inline_xbrl import InlineXbrl
    from data_engine.datahub.standards.segment_extraction import single_segment_description

    def note(name: str) -> str | None:
        return InlineXbrl((FILINGS / name).read_bytes()).text_block(
            "us-gaap:SegmentReportingDisclosureTextBlock", limit=600
        )

    shop = note("SHOP_10K_000159480526000007.html")
    assert shop is not None and shop.startswith("Segment and Geographical Information The Company")
    assert "operates in one single operating and reportable segment" in shop
    avgo = note("AVGO_10K_000173016825000121.html")
    assert avgo is not None and "two reportable segments: semiconductor solutions and infrastructure software" in avgo
    assert note("DDOG_10K_000162828026008819.html") is None

    visa = (FILINGS / "V_10K_000140316125000089.html").read_bytes()
    document = InlineXbrl(visa)
    declared = declared_segment_count(document)
    assert declared is not None and declared.statement is not None
    assert "Significant expenses" in declared.statement
    description = single_segment_description(document, declared, visa)
    assert description is not None
    assert "The Company has one reportable segment, Payment Services." in description
    assert "Significant expenses" not in description

    # #849: the business opening rides along when the filer tags one. SHOP's nature-of-
    # operations block; DDOG tags no segment note at all, so its declaration is the prose
    # pattern's, and its organization note still says what it does.
    shop_bytes = (FILINGS / "SHOP_10K_000159480526000007.html").read_bytes()
    shop_doc = InlineXbrl(shop_bytes)
    shop_declared = declared_segment_count(shop_doc)
    assert shop_declared is not None
    shop_description = single_segment_description(shop_doc, shop_declared, shop_bytes)
    assert shop_description is not None
    assert "operates in one single operating and reportable segment" in shop_description
    assert " Business: Nature of Business Shopify Inc." in shop_description
    assert "Shopify provides essential internet infrastructure for commerce" in shop_description

    ddog_bytes = (FILINGS / "DDOG_10K_000162828026008819.html").read_bytes()
    ddog_doc = InlineXbrl(ddog_bytes)
    ddog_declared = declared_segment_count(ddog_doc)
    assert ddog_declared is not None and ddog_declared.statement is None
    ddog_description = single_segment_description(ddog_doc, ddog_declared, ddog_bytes)
    assert ddog_description is not None
    assert "AI-powered observability" in ddog_description.split(" Business: ", 1)[1]


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


def test_geographic_business_segments_are_refused_and_product_axis_fallback_balances() -> None:
    """#1642 Q6: When all members of `StatementBusinessSegmentsAxis` are geographic regions
    (`Americas`, `Europe`, `Greater China`, `Japan`, `Rest of Asia Pacific`), that candidate set
    is refused with `geographic_axis_members`, and `tagged_segment_revenues` /
    `accepted_tagged_partition` selects the balancing `srt:ProductOrServiceAxis` breakdown."""
    for geo_member in (
        "aapl:AmericasSegmentMember",
        "aapl:EuropeSegmentMember",
        "aapl:GreaterChinaSegmentMember",
        "aapl:JapanSegmentMember",
        "country:JP",
        "aapl:RestOfAsiaPacificSegmentMember",
    ):
        assert _is_geographic_member(geo_member) is True
    assert _is_geographic_member("aapl:IPhoneMember") is False
    assert _is_geographic_member("avgo:SemiconductorSolutionsMember") is False

    aapl_tagged = tagged_segment_revenues((FILINGS / "AAPL_10K_000032019325000079.html").read_bytes())
    assert len(aapl_tagged) == 2
    geo_set, prod_set = aapl_tagged
    assert geo_set.shape == "all"
    assert geo_set.refusal == "geographic_axis_members"
    assert geo_set.parts == ()

    assert prod_set.shape == "product-axis"
    assert prod_set.refusal is None
    assert [m for m, _ in prod_set.parts] == [
        "aapl:IPadMember",
        "aapl:IPhoneMember",
        "aapl:MacMember",
        "aapl:WearablesHomeandAccessoriesMember",
        "us-gaap:ServiceMember",
    ]
    assert sum((v for _, v in prod_set.parts), Decimal(0)) == Decimal("416161000000")

    accepted = accepted_tagged_partition(
        aapl_tagged,
        total=Decimal("416161000000"),
        period_end=prod_set.period_end,
    )
    assert isinstance(accepted, TaggedPartition)
    assert accepted.segments.shape == "product-axis"
    assert dict(accepted.parts) == {
        "aapl:IPadMember": Decimal("28023000000"),
        "aapl:IPhoneMember": Decimal("209586000000"),
        "aapl:MacMember": Decimal("33708000000"),
        "aapl:WearablesHomeandAccessoriesMember": Decimal("35686000000"),
        "us-gaap:ServiceMember": Decimal("109158000000"),
    }


EXCERPTS = FILINGS / "segment_excerpts"


def test_subtotals_and_second_spellings_on_the_segment_axis_are_not_segments() -> None:
    """#1851, staging 2026-11-24: about 76 S&P 500 sets summed over consolidated revenue, many by
    about 100 %, because the filer also tags a subtotal on the segment axis (MLM
    `ReportableSegmentsMember` = all, `BuildingMaterialsBusinessMember` = East + West) or one
    segment under two spellings (COO). The excerpts are the filers' own tags, verbatim."""
    mlm = tagged_segment_revenues((EXCERPTS / "MLM_10K_000119312526059193_revenue.html").read_bytes())[0]
    assert {m.split(":")[-1] for m in mlm.subtotals} == {"ReportableSegmentsMember", "BuildingMaterialsBusinessMember"}
    assert len(mlm.parts) == 5, "the set as tagged stays; the variant is tried only after it fails"
    assert sum((v for _, v in mlm.without_subtotals), Decimal(0)) == Decimal("6150000000")
    accepted = accepted_tagged_partition([mlm], total=Decimal("6150000000"), period_end=mlm.period_end)
    assert isinstance(accepted, TaggedPartition) and accepted.verdict.residual == 0
    assert accepted.segments.subtotals_removed and len(accepted.parts) == 3
    assert accepted.extractor == RULE_SUBTOTAL_FREE_PARTITION

    coo = tagged_segment_revenues((EXCERPTS / "COO_10K_000162828025055615_revenue.html").read_bytes())[0]
    assert len(coo.without_subtotals) == 2 and len(coo.subtotals) == 2
    accepted = accepted_tagged_partition([coo], total=Decimal("4092400000"), period_end=coo.period_end)
    assert isinstance(accepted, TaggedPartition) and sum((v for _, v in accepted.parts), Decimal(0)) == Decimal(
        "4092400000"
    )


@pytest.mark.parametrize(
    ("name", "total", "subtotal", "elimination"),
    [
        ("WEC_10K_000078332526000018_revenue.html", "9800100000", "PublicUtilityMember", "-476700000"),
        (
            "INTC_10K_000005086326000011_revenue.html",
            "52853000000",
            "ClientComputingGroupDatacenterAndAIAndNetworkAndEdgeMember",
            "-17683000000",
        ),
    ],
)
def test_a_subtotal_out_and_the_tagged_elimination_in_balance_the_segments(
    name: str, total: str, subtotal: str, elimination: str
) -> None:
    tagged = tagged_segment_revenues((EXCERPTS / name).read_bytes())
    assert subtotal in {m.split(":")[-1] for m in tagged[0].subtotals}
    accepted = accepted_tagged_partition(tagged, total=Decimal(total), period_end=tagged[0].period_end)
    assert isinstance(accepted, TaggedPartition), accepted
    assert accepted.elimination is not None
    assert accepted.extractor == RULE_SUBTOTAL_FREE_ELIMINATION_PARTITION
    assert accepted.verdict.residual == Decimal(total) - sum((v for _, v in accepted.parts), Decimal(0))
    assert abs(accepted.verdict.residual - Decimal(elimination)) <= Decimal("5000000")


def test_a_segment_that_merely_equals_one_other_is_no_subtotal() -> None:
    """A subtotal is the sum of two or more members; two segments of equal revenue both stay."""
    from data_engine.datahub.standards.segment_extraction import _without_subtotals

    parts = (("x:AlphaMember", Decimal("100")), ("x:BetaMember", Decimal("100")), ("x:GammaMember", Decimal("50")))
    kept, dropped = _without_subtotals(parts, Decimal("5"))
    assert kept == parts and dropped == ()


def test_a_set_that_balances_as_tagged_is_taken_as_tagged() -> None:
    """#1851 replay on staging: LIN's Americas segment (15,208M) is within 2M of APAC plus EMEA
    (15,210M), and a loose bound dropped it as a subtotal, so a set that balanced before was
    refused. The set as tagged is tried first, and the bound is half a printed unit per figure."""
    from datetime import date

    from data_engine.datahub.standards.segment_extraction import TaggedSegments, _is_sum_of_others

    unit = Decimal("1000000")
    assert not _is_sum_of_others(Decimal("15208") * unit, [Decimal("6661") * unit, Decimal("8549") * unit], unit)
    assert _is_sum_of_others(
        Decimal("33079") * unit,
        [Decimal("13976") * unit, Decimal("8815") * unit, Decimal("10287") * unit],
        unit,
    ), "MDT: three segments rounded to the million sum to 33,078M against a 33,079M subtotal"

    lin = TaggedSegments(
        concept="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        shape="all",
        period_end=date(2025, 12, 31),
        parts=(
            ("lin:APACSegmentMember", Decimal("6661000000")),
            ("lin:AmericasSegmentMember", Decimal("15208000000")),
            ("lin:EMEASegmentMember", Decimal("8549000000")),
            ("lin:EngineeringSegmentMember", Decimal("2250000000")),
            ("us-gaap:CorporateAndOtherMember", Decimal("1318000000")),
        ),
        scale=6,
    )
    accepted = accepted_tagged_partition([lin], total=Decimal("33986000000"), period_end=date(2025, 12, 31))
    assert isinstance(accepted, TaggedPartition)
    assert len(accepted.parts) == 5 and not accepted.segments.subtotals_removed


def _set(parts, *, reconciling=(), eliminations=(), without=(), subtotals=()) -> TaggedSegments:
    return TaggedSegments(
        concept="us-gaap:Revenues",
        shape="all",
        period_end=date(2025, 12, 31),
        parts=tuple(parts),
        scale=0,
        reconciling=tuple(reconciling),
        eliminations=tuple(eliminations),
        without_subtotals=tuple(without),
        subtotals=tuple(subtotals),
    )


def test_the_set_as_tagged_wins_over_its_variant_without_subtotals() -> None:
    """A balances as B + C, and the set as tagged balances 200. The variant without A balances
    too, with the reconciling item, but it is tried only after every set as tagged fails."""
    tagged = _set(
        [("x:AMember", Decimal(100)), ("x:BMember", Decimal(60)), ("x:CMember", Decimal(40))],
        reconciling=[("us-gaap:CorporateNonSegmentMember", Decimal(100))],
        without=[("x:BMember", Decimal(60)), ("x:CMember", Decimal(40))],
        subtotals=["x:AMember"],
    )
    accepted = accepted_tagged_partition([tagged], total=Decimal(200), period_end=tagged.period_end)
    assert isinstance(accepted, TaggedPartition)
    assert [m for m, _ in accepted.parts] == ["x:AMember", "x:BMember", "x:CMember"]
    assert accepted.reconciling is None and not accepted.segments.subtotals_removed
    assert accepted.extractor == RULE_EXHAUSTIVE_PARTITION


def test_a_tagged_elimination_wins_over_a_subtotal_inference() -> None:
    """Both close 200: the elimination is the filer's own fact, the subtotal an inference from sums."""
    tagged = _set(
        [("x:AMember", Decimal(150)), ("x:BMember", Decimal(100)), ("x:CMember", Decimal(50))],
        eliminations=[("us-gaap:IntersegmentEliminationMember", Decimal(-100))],
        without=[("x:AMember", Decimal(150)), ("x:CMember", Decimal(50))],
        subtotals=["x:BMember"],
    )
    accepted = accepted_tagged_partition([tagged], total=Decimal(200), period_end=tagged.period_end)
    assert isinstance(accepted, TaggedPartition)
    assert accepted.elimination == "us-gaap:IntersegmentEliminationMember"
    assert accepted.verdict.residual == Decimal(-100) and accepted.extractor == RULE_ELIMINATION_PARTITION


def _fact(member: str, value: str) -> SimpleNamespace:
    return SimpleNamespace(value=Decimal(value), context=SimpleNamespace(dimensions=((CONSOLIDATION_AXIS, member),)))


def test_only_a_negative_intersegment_elimination_is_an_elimination() -> None:
    facts = [
        _fact("us-gaap:IntersegmentEliminationMember", "-476700000"),
        _fact("us-gaap:MaterialReconcilingItemsMember", "-90"),
        _fact("x:InterSegmentEliminationsMember", "120"),
        _fact("x:CorporateReconcilingItemsAndEliminationsMember", "-300"),
        _fact("x:OtherEliminationMember", "-5"),
    ]
    assert _eliminations(facts) == (("us-gaap:IntersegmentEliminationMember", Decimal("-476700000")),)
    twice = [_fact("x:EliminationsMember", "-10"), _fact("x:EliminationsMember", "-12")]
    assert _eliminations(twice) == (), "one member with two values is no fact"


def test_a_sum_of_two_members_is_found_whatever_their_order() -> None:
    assert _is_sum_of_others(Decimal(100), [Decimal(100), Decimal(50), Decimal(50)], Decimal(1))
    assert _is_sum_of_others(Decimal(100), [Decimal(50), Decimal(50), Decimal(100)], Decimal(1))
    for names in (
        ("x:AMember", "x:BMember", "x:CMember", "x:DMember"),
        ("x:YMember", "x:ZMember", "x:AMember", "x:BMember"),
    ):
        parts = tuple(zip(names, (Decimal(100), Decimal(100), Decimal(50), Decimal(50)), strict=True))
        kept, dropped = _without_subtotals(tuple(sorted(parts)), Decimal(1))
        assert sorted(v for _, v in kept) == [Decimal(50), Decimal(50)] and len(dropped) == 2


@pytest.mark.parametrize(
    ("rule", "confidence"),
    [
        (RULE_EXHAUSTIVE_PARTITION, "0.90"),
        (RULE_ELIMINATION_PARTITION, "0.85"),
        (RULE_SUBTOTAL_FREE_PARTITION, "0.80"),
        (RULE_SUBTOTAL_FREE_ELIMINATION_PARTITION, "0.80"),
    ],
)
def test_every_partition_rule_has_its_own_confidence(rule: str, confidence: str) -> None:
    """Each rule lands under its own extractor, so `withdrawn_extractors` can retract one rule."""
    assert confidence_for("segment-revenue-confidence:v1", rule) == Decimal(confidence)
