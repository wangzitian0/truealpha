"""The depositary ratio read from a filing cover (#1519, step 1a).

The parser is pure: filing bytes and a ticker in, one `ListingUnitRatio` out. These tests judge it
three ways. Synthetic covers fix the three outcomes that the issue names: a depositary quantity, a
plain share title and an unfamiliar title. The packaged real filings of `samples/filings/` measure
what the closed pattern set reads today. The reader tests judge the memo and the error path.

The packaged filings are part of the repository. A missing file fails the test and never skips it.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from data_engine.datahub.standards.filing_extraction import FilingDocument
from data_engine.datahub.standards.listing_unit_ratio import (
    OUTCOME_NOT_MEASURED,
    OUTCOME_ORDINARY,
    OUTCOME_PARSED,
    OUTCOME_UNPARSED,
    PATTERN_DEPOSITARY,
    PATTERN_PLAIN_SHARE,
    PATTERN_UNPARSED,
    PAYLOAD_KEYS,
    REASON_DEPOSITARY_WITHOUT_QUANTITY,
    REASON_NO_ANNUAL_FILING,
    REASON_NO_TICKER,
    REASON_READ_SKIPPED,
    REASON_TICKER_NOT_ON_COVER,
    REASON_TITLES_DISAGREE,
    REASON_UNFAMILIAR_TITLE,
    CoverRatioReader,
    ListingUnitRatio,
    cover_securities,
    outcome_of_payload,
    ratio_for_ticker,
    read_title,
)

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from cover_fixture import cover_html  # noqa: E402

FILINGS = Path(__file__).resolve().parents[1] / "samples" / "filings"
ACCESSION = "0001234567-26-000010"
FORM = "20-F"
FILED = date(2026, 4, 22)


def _ratio(body: bytes, ticker: str) -> ListingUnitRatio:
    return ratio_for_ticker(cover_securities(body), ticker, accession=ACCESSION, form=FORM, filed=FILED)


# --- criterion (a): the three outcomes that the issue names ------------------------------------


def test_a_depositary_title_with_a_quantity_gives_that_quantity_and_names_its_pattern() -> None:
    """PDD-shaped cover: "each representing four Class A ordinary shares" gives 4.

    The title has a line break and a no-break space, as the printed 20-F has. Red on main: the module
    does not exist, so nothing reads the title."""
    title = "American Depositary Shares, each representing<br/>four&#160;Class A ordinary shares, par value US$0.00002 per share"

    ratio = _ratio(cover_html(("PDD", title)), "PDD")

    assert ratio.listing_units_per_share == Decimal(4)
    assert ratio.pattern == PATTERN_DEPOSITARY
    assert ratio.outcome == OUTCOME_PARSED
    assert (
        ratio.title
        == "American Depositary Shares, each representing four Class A ordinary shares, par value US$0.00002 per share"
    )
    assert (ratio.accession, ratio.form, ratio.filed) == (ACCESSION, FORM, FILED)
    assert ratio.reason is None


def test_a_plain_share_title_gives_one() -> None:
    ratio = _ratio(cover_html(("ACME", "Common Stock, $0.00001 par value per share")), "ACME")

    assert ratio.listing_units_per_share == Decimal(1)
    assert ratio.pattern == PATTERN_PLAIN_SHARE
    assert ratio.outcome == OUTCOME_ORDINARY
    assert ratio.title == "Common Stock, $0.00001 par value per share"
    assert ratio.reason is None


def test_an_unfamiliar_title_gives_no_value_and_keeps_its_text() -> None:
    title = "Units, each consisting of one share of Class A common stock and one-third of one redeemable warrant"

    ratio = _ratio(cover_html(("ACME", title)), "ACME")

    assert ratio.listing_units_per_share is None
    assert ratio.pattern == PATTERN_UNPARSED
    assert ratio.outcome == OUTCOME_UNPARSED
    assert ratio.reason == REASON_UNFAMILIAR_TITLE
    assert ratio.title == title
    assert (ratio.accession, ratio.filed) == (ACCESSION, FILED)


@pytest.mark.parametrize(
    ("title", "units"),
    [
        ("American Depositary Shares, each representing one Ordinary Share", 1),
        ("American Depositary Shares, each representing two ordinary shares", 2),
        ("American Depositary Shares each representing 3 Class B ordinary shares", 3),
        ("American Depositary Shares, each representing ten common shares, no par value", 10),
        ("American Depositary Shares, each representing twenty-five ordinary shares (nominal value 0.01)", 25),
        ("ADSs, each representing one hundred Class A ordinary shares", 100),
        ("American Depositary Shares, each ADS representing five ordinary shares of par value NIS 0.1", 5),
        ("American Depositary Shares, each representing the right to receive four ordinary shares", 4),
    ],
)
def test_the_quantity_of_a_depositary_title_is_the_number_it_states(title: str, units: int) -> None:
    reading = read_title(title)

    assert reading.listing_units_per_share == Decimal(units), title
    assert reading.pattern == PATTERN_DEPOSITARY


@pytest.mark.parametrize(
    "title",
    [
        "Common Stock, par value $0.01 per share",
        "Class A common stock, par value $0.00001 per share",
        "Class A Subordinate Voting Shares",
        "Class C Capital Stock, $0.001 par value",
        "Ordinary shares (nominal value EUR 0.09 per share)",
        "Common Shares, without par value",
        "Common Stock $0.01 par value",
        "Common stock",
    ],
)
def test_a_plain_share_title_is_one_ordinary_share(title: str) -> None:
    reading = read_title(title)

    assert reading.listing_units_per_share == Decimal(1), title
    assert reading.pattern == PATTERN_PLAIN_SHARE


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        # A fraction is a quantity, and the closed set has no pattern for it.
        ("American Depositary Shares, each representing one-half of one Ordinary Share", REASON_UNFAMILIAR_TITLE),
        # A depositary share of a preferred share is not an ordinary-share ratio.
        (
            "Depositary Shares, each representing a one-four hundredth interest in a share of 5.75% Preferred Stock",
            REASON_UNFAMILIAR_TITLE,
        ),
        (
            "Depositary Shares, each representing one share of 6.00% Non-Cumulative Preferred Stock",
            REASON_UNFAMILIAR_TITLE,
        ),
        ("American Depositary Shares", REASON_DEPOSITARY_WITHOUT_QUANTITY),
        ("American Depositary Shares, each representing zero ordinary shares", REASON_UNFAMILIAR_TITLE),
        ("American Depositary Shares, each representing four", REASON_UNFAMILIAR_TITLE),
        ("1.625% Notes due 2026", REASON_UNFAMILIAR_TITLE),
        ("Class A Common Stock and Class B Common Stock", REASON_UNFAMILIAR_TITLE),
        ("Warrants, each exercisable for one share of common stock", REASON_UNFAMILIAR_TITLE),
        ("", REASON_UNFAMILIAR_TITLE),
    ],
)
def test_any_other_title_gives_no_value(title: str, reason: str) -> None:
    reading = read_title(title)

    assert reading.listing_units_per_share is None, title
    assert reading.pattern == PATTERN_UNPARSED
    assert reading.reason == reason


def test_the_value_follows_the_stated_quantity() -> None:
    """Mutation guard: a parser that returns a fixed number for a depositary title fails here."""
    four = read_title("American Depositary Shares, each representing four ordinary shares")
    seven = read_title("American Depositary Shares, each representing seven ordinary shares")

    assert (four.listing_units_per_share, seven.listing_units_per_share) == (Decimal(4), Decimal(7))


# --- the pairing of title and ticker ------------------------------------------------------------


def test_a_title_pairs_with_the_ticker_of_its_own_context() -> None:
    """Visa-shaped cover: one common line and several notes. Each ticker reads its own line."""
    body = cover_html(
        ("V", "Class A Common Stock, par value $0.0001 per share"),
        ("V26", "1.500% Senior Notes due 2026"),
        ("PDD", "American Depositary Shares, each representing four Class A ordinary shares"),
    )

    assert _ratio(body, "V").listing_units_per_share == Decimal(1)
    assert _ratio(body, "PDD").listing_units_per_share == Decimal(4)
    assert _ratio(body, "V26").reason == REASON_UNFAMILIAR_TITLE


def test_a_title_without_a_ticker_in_its_context_is_not_a_cover_line() -> None:
    """AAPL-shaped cover: the notes have a title and no ticker. They never answer for a ticker."""
    body = cover_html(("AAPL", "Common Stock, $0.00001 par value per share"), (None, "0.000% Notes due 2025"))

    assert [(line.symbol, line.title) for line in cover_securities(body)] == [
        ("AAPL", "Common Stock, $0.00001 par value per share")
    ]


def test_a_ticker_that_is_not_on_the_cover_gives_no_value() -> None:
    ratio = _ratio(cover_html(("AAPL", "Common Stock, $0.00001 par value per share")), "MSFT")

    assert ratio.listing_units_per_share is None
    assert ratio.reason == REASON_TICKER_NOT_ON_COVER
    assert ratio.title is None


def test_a_share_class_dot_and_hyphen_name_one_ticker() -> None:
    body = cover_html(("BRK-B", "Class B Common Stock, $0.0033 par value"))

    assert _ratio(body, "BRK.B").listing_units_per_share == Decimal(1)


def test_two_lines_under_one_ticker_must_agree() -> None:
    """The cover of an ADS can list two lines under one ticker (#1519, U7)."""
    agree = cover_html(
        ("ABC", "American Depositary Shares, each representing four ordinary shares"),
        ("ABC", "American Depositary Shares, each representing four Class A ordinary shares"),
    )
    differ = cover_html(
        ("ABC", "American Depositary Shares, each representing four ordinary shares"),
        ("ABC", "American Depositary Shares, each representing five ordinary shares"),
    )
    half_parsed = cover_html(
        ("ABC", "American Depositary Shares, each representing four ordinary shares"),
        ("ABC", "Units, each consisting of one share and one warrant"),
    )

    assert _ratio(agree, "ABC").listing_units_per_share == Decimal(4)
    assert _ratio(differ, "ABC").listing_units_per_share is None
    assert _ratio(differ, "ABC").reason == REASON_TITLES_DISAGREE
    assert _ratio(half_parsed, "ABC").listing_units_per_share is None


def test_a_context_with_two_tickers_cannot_be_paired() -> None:
    body = (
        b'<ix:nonNumeric contextRef="c-1" name="dei:Security12bTitle">Common Stock</ix:nonNumeric>'
        b'<ix:nonNumeric contextRef="c-1" name="dei:TradingSymbol">AAA</ix:nonNumeric>'
        b'<ix:nonNumeric contextRef="c-1" name="dei:TradingSymbol">BBB</ix:nonNumeric>'
    )

    assert cover_securities(body) == ()


def test_an_element_that_never_closes_is_not_read() -> None:
    body = b'<ix:nonNumeric contextRef="c-1" name="dei:Security12bTitle">Common Stock'

    assert cover_securities(body) == ()


# --- the packaged real filings: what the closed set reads today ---------------------------------

#: (sample file, ticker, outcome, value, pattern, reason). The measured result for the repository's real filings.
SAMPLES = [
    ("AAPL_10K_000032019325000079.html", "AAPL", OUTCOME_ORDINARY, Decimal(1), PATTERN_PLAIN_SHARE, None),
    ("ADM_10K_000000708426000011.html", "ADM", OUTCOME_ORDINARY, Decimal(1), PATTERN_PLAIN_SHARE, None),
    ("ADP_10K_000000867026000030.html", "ADP", OUTCOME_ORDINARY, Decimal(1), PATTERN_PLAIN_SHARE, None),
    ("AVGO_10K_000173016825000121.html", "AVGO", OUTCOME_ORDINARY, Decimal(1), PATTERN_PLAIN_SHARE, None),
    ("DDOG_10K_000162828026008819.html", "DDOG", OUTCOME_ORDINARY, Decimal(1), PATTERN_PLAIN_SHARE, None),
    ("DUOL_10K_000162828026012494.html", "DUOL", OUTCOME_ORDINARY, Decimal(1), PATTERN_PLAIN_SHARE, None),
    ("JPM_10K_000162828026008131.html", "JPM", OUTCOME_ORDINARY, Decimal(1), PATTERN_PLAIN_SHARE, None),
    ("NICE_20F_000100393526000010.html", "NICE", OUTCOME_PARSED, Decimal(1), PATTERN_DEPOSITARY, None),
    ("NVDA_8K_SPLIT_000104581024000144.html", "NVDA", OUTCOME_ORDINARY, Decimal(1), PATTERN_PLAIN_SHARE, None),
    ("PLUG_10K_000155837021007147.html", "PLUG", OUTCOME_ORDINARY, Decimal(1), PATTERN_PLAIN_SHARE, None),
    ("PLUG_10KA_000155837022003577.html", "PLUG", OUTCOME_ORDINARY, Decimal(1), PATTERN_PLAIN_SHARE, None),
    ("SHOP_10K_000159480526000007.html", "SHOP", OUTCOME_ORDINARY, Decimal(1), PATTERN_PLAIN_SHARE, None),
    ("V_10K_000140316125000089.html", "V", OUTCOME_ORDINARY, Decimal(1), PATTERN_PLAIN_SHARE, None),
    # The two files below carry no tagged cover. The 8-K of Meta is the plain document of a symbol change.
    (
        "META_8K_SYMBOL_000132680122000070.html",
        "META",
        OUTCOME_UNPARSED,
        None,
        PATTERN_UNPARSED,
        REASON_TICKER_NOT_ON_COVER,
    ),
    (
        "NVDA_8K_GUIDANCE_000104581024000113.html",
        "NVDA",
        OUTCOME_UNPARSED,
        None,
        PATTERN_UNPARSED,
        REASON_TICKER_NOT_ON_COVER,
    ),
]


@pytest.mark.parametrize(
    ("name", "ticker", "outcome", "value", "pattern", "reason"), SAMPLES, ids=[row[0] for row in SAMPLES]
)
def test_the_closed_set_reads_the_packaged_real_filings(
    name: str, ticker: str, outcome: str, value: Decimal | None, pattern: str, reason: str | None
) -> None:
    body = (FILINGS / name).read_bytes()

    ratio = ratio_for_ticker(cover_securities(body), ticker, accession=ACCESSION, form=FORM, filed=FILED)

    assert (ratio.outcome, ratio.listing_units_per_share, ratio.pattern, ratio.reason) == (
        outcome,
        value,
        pattern,
        reason,
    ), ratio


def test_the_nice_cover_reads_its_own_title_and_not_a_guess() -> None:
    ratio = _ratio((FILINGS / "NICE_20F_000100393526000010.html").read_bytes(), "NICE")

    assert ratio.title == (
        "American Depositary Shares, each representing one Ordinary Share, par value one New Israeli Shekel per share"
    )


def test_the_sample_set_has_no_file_that_the_table_omits() -> None:
    """A new sample file must get a row in `SAMPLES`, so the measured table stays whole."""
    in_table = {row[0] for row in SAMPLES}
    on_disk = {path.name for path in FILINGS.glob("*.html")}

    assert on_disk - in_table == set()


# --- the outcome record -------------------------------------------------------------------------


def test_the_payload_fields_are_the_declared_keys_and_the_filing_date_is_iso() -> None:
    parsed = _ratio(cover_html(("PDD", "American Depositary Shares, each representing four ordinary shares")), "PDD")

    fields = parsed.payload_fields()

    assert tuple(fields) == PAYLOAD_KEYS
    assert fields["listing_units_per_share"] == "4"
    assert fields["ratio_pattern"] == PATTERN_DEPOSITARY
    assert fields["ratio_accession"] == ACCESSION
    assert fields["ratio_filed"] == "2026-04-22"
    assert fields["ratio_reason"] is None


def test_an_outcome_cannot_hold_a_value_and_a_reason() -> None:
    with pytest.raises(ValueError, match="inconsistent ratio outcome"):
        ListingUnitRatio(Decimal(4), PATTERN_DEPOSITARY, reason=REASON_UNFAMILIAR_TITLE)
    with pytest.raises(ValueError, match="inconsistent ratio outcome"):
        ListingUnitRatio(None, PATTERN_UNPARSED)
    with pytest.raises(ValueError, match="inconsistent ratio outcome"):
        ListingUnitRatio(Decimal(1), PATTERN_UNPARSED)


def test_a_payload_without_a_ratio_key_is_not_measured_and_an_unparsed_payload_is_not() -> None:
    parsed = _ratio(cover_html(("PDD", "American Depositary Shares, each representing four ordinary shares")), "PDD")
    plain = _ratio(cover_html(("ACME", "Common stock")), "ACME")
    unparsed = _ratio(cover_html(("ACME", "Rights")), "ACME")

    assert outcome_of_payload({"revenue": "1"}) == OUTCOME_NOT_MEASURED
    assert outcome_of_payload(parsed.payload_fields()) == OUTCOME_PARSED
    assert outcome_of_payload(plain.payload_fields()) == OUTCOME_ORDINARY
    assert outcome_of_payload(unparsed.payload_fields()) == OUTCOME_UNPARSED


# --- the reader: one read per issuer, and an error never aborts ---------------------------------


def _document(body: bytes, *, cik: int = 1737806) -> FilingDocument:
    return FilingDocument(
        cik=cik,
        accession="000123456726000010",
        form=FORM,
        filing_date=FILED,
        primary_document="pdd-20f.htm",
        url="https://www.sec.gov/Archives/edgar/data/1737806/000123456726000010/pdd-20f.htm",
        body=body,
    )


def test_the_reader_formats_the_accession_and_asks_for_the_end_of_the_cutoff_day() -> None:
    asked: list[tuple[int, datetime]] = []
    body = cover_html(("PDD", "American Depositary Shares, each representing four Class A ordinary shares"))

    def find(cik: int, cutoff: datetime) -> FilingDocument:
        asked.append((cik, cutoff))
        return _document(body)

    ratio = CoverRatioReader(find)(1737806, date(2026, 5, 4), "PDD")

    assert ratio.listing_units_per_share == Decimal(4)
    assert ratio.accession == "0001234567-26-000010"
    assert (ratio.form, ratio.filed) == (FORM, FILED)
    assert asked == [(1737806, datetime(2026, 5, 4, 23, 59, 59, 999999, tzinfo=UTC))]


def test_two_listings_of_one_issuer_read_one_filing() -> None:
    calls: list[int] = []
    body = cover_html(
        ("GOOGL", "Class A Common Stock, $0.001 par value"), ("GOOG", "Class C Capital Stock, $0.001 par value")
    )

    def find(cik: int, cutoff: datetime) -> FilingDocument:
        calls.append(cik)
        return _document(body, cik=cik)

    reader = CoverRatioReader(find)
    first = reader(1652044, date(2026, 5, 4), "GOOGL")
    second = reader(1652044, date(2026, 5, 4), "GOOG")
    reader(1652044, date(2026, 5, 5), "GOOG")

    assert (first.listing_units_per_share, second.listing_units_per_share) == (Decimal(1), Decimal(1))
    assert calls == [1652044, 1652044], "one read per (CIK, cutoff), not one per listing"


def test_a_read_that_fails_gives_an_unparsed_outcome_that_names_the_error_and_never_raises() -> None:
    def find(_cik: int, _cutoff: datetime) -> FilingDocument:
        raise ConnectionError("SEC is down")

    ratio = CoverRatioReader(find)(1737806, date(2026, 5, 4), "PDD")

    assert ratio.listing_units_per_share is None
    assert ratio.reason == "read_failed:ConnectionError"
    assert ratio.pattern == PATTERN_UNPARSED


def test_an_issuer_with_no_annual_filing_and_a_listing_with_no_ticker_give_named_reasons() -> None:
    reader = CoverRatioReader(lambda _cik, _cutoff: None)

    assert reader(1737806, date(2026, 5, 4), "PDD").reason == REASON_NO_ANNUAL_FILING
    assert reader(1737806, date(2026, 5, 4), " ").reason == REASON_NO_TICKER


# --- the outage breaker: a host that is down costs three reads, not one per issuer -------------


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://www.sec.gov/Archives/edgar/data/1/1/doc.htm")
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=httpx.Response(status, request=request))


def _reader_over(errors: list[Exception | None]) -> tuple[CoverRatioReader, list[int]]:
    """A reader whose k-th read raises `errors[k]` or, for None, finds no filing. The list names the CIKs asked."""
    asked: list[int] = []

    def find(cik: int, _cutoff: datetime) -> FilingDocument | None:
        asked.append(cik)
        error = errors[len(asked) - 1]
        if error is not None:
            raise error
        return None

    return CoverRatioReader(find), asked


def test_three_failures_that_the_next_issuer_may_share_end_the_reads_of_the_run() -> None:
    """A timeout, a block and a 5xx may repeat. Each read waits for the 30 s SEC timeout, so the fourth issuer is
    not asked. The outcome names the reason and the next run reads again."""
    reader, asked = _reader_over([httpx.ReadTimeout("slow"), _status_error(403), _status_error(503)])

    outcomes = [reader(cik, date(2026, 5, 4), "ACME") for cik in (1, 2, 3, 4, 5)]

    assert [o.reason for o in outcomes] == [
        "read_failed:ReadTimeout",
        "read_failed:HTTPStatusError",
        "read_failed:HTTPStatusError",
        REASON_READ_SKIPPED,
        REASON_READ_SKIPPED,
    ]
    assert asked == [1, 2, 3], "the fourth and fifth issuers make no request"
    fresh, fresh_asked = _reader_over([None])
    assert fresh(4, date(2026, 5, 4), "ACME").reason == REASON_NO_ANNUAL_FILING
    assert fresh_asked == [4]


def test_a_missing_document_and_a_faulty_document_never_end_the_reads() -> None:
    """A 404 is one issuer's missing document, and a parse fault is one document's fault. Neither repeats."""
    reader, asked = _reader_over([_status_error(404)] * 4 + [ValueError("bad document")] * 4)

    outcomes = [reader(cik, date(2026, 5, 4), "ACME") for cik in range(1, 9)]

    assert asked == list(range(1, 9)), "every issuer is asked"
    assert REASON_READ_SKIPPED not in {o.reason for o in outcomes}


def test_a_read_that_works_resets_the_count_of_failures_in_a_row() -> None:
    reader, asked = _reader_over([httpx.ReadTimeout("slow")] * 2 + [None] + [httpx.ReadTimeout("slow")] * 2 + [None])

    outcomes = [reader(cik, date(2026, 5, 4), "ACME") for cik in range(1, 7)]

    assert asked == [1, 2, 3, 4, 5, 6]
    assert REASON_READ_SKIPPED not in {o.reason for o in outcomes}


# --- the dashboard row: one cell per listing, counted by the engine --------------------------------------------


def test_the_report_cell_of_a_payload_carries_the_outcome_and_the_evidence() -> None:
    from data_engine.datahub.quality_report import listing_ratio_cell, listing_ratio_report

    parsed = _ratio(cover_html(("PDD", "American Depositary Shares, each representing four ordinary shares")), "PDD")
    payload = {"issuer_id": "issuer:cik:1737806", "listing_id": "listing:xnas:pdd", **parsed.payload_fields()}

    cell = listing_ratio_cell(payload)

    assert cell == {
        "issuer_id": "issuer:cik:1737806",
        "listing_id": "listing:xnas:pdd",
        "outcome": OUTCOME_PARSED,
        "listing_units_per_share": "4",
        "pattern": PATTERN_DEPOSITARY,
        "title": "American Depositary Shares, each representing four ordinary shares",
        "reason": None,
        "accession": ACCESSION,
        "form": FORM,
        "filed": "2026-04-22",
    }
    block = listing_ratio_report({"b": listing_ratio_cell({"listing_id": "b"}), "a": cell})
    assert block["counts"] == {"parsed": 1, "ordinary": 0, "unparsed": 0, "not_measured": 1}
    assert list(block["cells"]) == ["a", "b"], (
        "the cells are in a fixed order, so the report hash does not depend on it"
    )


def test_a_payload_without_ratio_keys_is_a_not_measured_cell_with_no_evidence() -> None:
    from data_engine.datahub.quality_report import listing_ratio_cell

    cell = listing_ratio_cell({"issuer_id": "issuer:cik:1", "listing_id": "listing:xnas:acme", "revenue": "1"})

    assert cell["outcome"] == OUTCOME_NOT_MEASURED
    assert {cell[key] for key in ("listing_units_per_share", "pattern", "title", "reason", "accession")} == {None}


def test_a_measured_cell_is_never_replaced_by_a_not_measured_one_of_the_same_subject() -> None:
    """The capture plane can bind several observations to one subject. A later payload without ratio keys must not
    erase the measurement of an earlier one. A later measurement does replace it."""
    from data_engine.datahub.quality_report import record_listing_ratio

    measured = _ratio(cover_html(("ACME", "Common stock")), "ACME").payload_fields()
    unparsed = _ratio(cover_html(("ACME", "Rights")), "ACME").payload_fields()
    cells: dict[str, dict] = {}

    record_listing_ratio(cells, "listing:xnas:acme", {"revenue": "1"})
    assert cells["listing:xnas:acme"]["outcome"] == OUTCOME_NOT_MEASURED
    record_listing_ratio(cells, "listing:xnas:acme", measured)
    assert cells["listing:xnas:acme"]["outcome"] == OUTCOME_ORDINARY
    record_listing_ratio(cells, "listing:xnas:acme", {"revenue": "1"})
    assert cells["listing:xnas:acme"]["outcome"] == OUTCOME_ORDINARY, "not measured does not erase a measurement"
    record_listing_ratio(cells, "listing:xnas:acme", unparsed)
    assert cells["listing:xnas:acme"]["outcome"] == OUTCOME_UNPARSED, "a later measurement replaces an earlier one"
