"""Real captured vendor bytes as parser fixtures (#539: shrink the test-reality gap).

Every cassette under ``cassettes/`` is the EXACT byte string production captured:
each file's sha256 is asserted against the content address ``raw.fetches`` recorded,
so a hand-edited or synthesized fixture turns this suite red. The expected values
are not this test author's either — they are what the deployed parsers wrote into
``staging.capture_observation_payloads`` for these exact bytes (staging, captured
2026-08-15/16 tick, reconciliation outcome: agreed).

Why this exists: on 2026-08-14 two premise bugs shipped green in one morning
because their tests faked the vendor exactly as the author imagined it — #557
(``urlopen`` raises on the HTTP 400 the live ``/eod`` actually returns; the fake
returned error bodies with status 200) and #553 (the fixture emitted only the two
operating branches the code happened to map). A fake can only encode the premise;
a cassette encodes the vendor. When a parser change breaks one of these, it is
breaking against reality, not against an assumption.

Refreshing: pick any recent ``raw.fetches`` row for the vendor, dereference its
``object_uri`` through the deployed store, verify the digest, and update
``_CASSETTES``. ``scripts/vendor_contract_smoke.py`` checks the other direction —
that the LIVE vendor still answers in the shape these bytes exhibit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from data_engine.datahub.production_topt.twelve_data_origin import parse_last_settled_close
from data_engine.datahub.quality_report import RECONCILIATION_POLICY
from data_engine.sources.yahoo import _parse_chart_response

_CASSETTES = Path(__file__).parent / "cassettes"

# (file, sha256 recorded in raw.fetches, byte_length recorded)
_YAHOO = (
    "yahoo_chart_aapl.a4c41945.json",
    "a4c41945303104e2bf2f9c8a1082cafbaaf1667f2382010fab7e44a5219b452e",
    28123,
)
_TWELVE = (
    "twelvedata_aapl.8ea52e37.json",
    "8ea52e376c54b076f252937535ce8be759ed7f2ffde0d56a31463b045df20abc",
    1022,
)

# What the deployed parsers recorded for these bytes (capture_observation_payloads):
#   yahoo      production-topt-live-parser:v4  -> close 305.93     (bar 2026-08-14)
#   twelvedata twelve-data-parser:v2           -> close 305.92999  (bar 2026-08-14)
_PARTITION = date(2026, 8, 15)


def _bytes(cassette: tuple[str, str, int]) -> bytes:
    name, sha, length = cassette
    body = (_CASSETTES / name).read_bytes()
    assert len(body) == length, f"{name}: {len(body)} bytes, raw.fetches recorded {length}"
    digest = hashlib.sha256(body).hexdigest()
    assert digest == sha, f"{name} is not the bytes production captured: sha256 {digest[:16]}…"
    return body


def test_cassettes_are_the_exact_bytes_production_captured() -> None:
    """Any edit to a cassette — reformatting, truncation, 'fixing' a value — fails here.

    The digest is the content address ``raw.fetches`` recorded, so the fixture's
    identity is anchored to the warehouse, not to this repo's history.
    """
    _bytes(_YAHOO)
    _bytes(_TWELVE)


def test_yahoo_parser_reproduces_the_production_close() -> None:
    """The deployed chart parser over Yahoo's verbatim bytes yields the recorded value.

    This also exercises float-recovery (#490) against reality: the raw body carries
    binary-float noise; the recorded normalized value is the quoted price.
    """
    # parse_float=Decimal mirrors `fetch_daily_chart`'s decode: the parser's contract
    # is "never route the response through binary float". Decoding with bare
    # json.loads here crashes recover_quoted_price — which is itself the contract
    # asserting it.
    bars = _parse_chart_response(json.loads(_bytes(_YAHOO), parse_float=Decimal))
    eligible = [bar for bar in bars if bar.date <= _PARTITION]
    assert eligible, "the cassette must contain bars at or before the capture partition"
    bar = max(eligible, key=lambda item: item.date)
    assert bar.date == date(2026, 8, 14)
    assert bar.close == Decimal("305.93")


def test_twelve_data_parser_reproduces_the_production_close() -> None:
    quote = parse_last_settled_close(_bytes(_TWELVE), partition=_PARTITION)
    assert quote is not None
    assert quote.as_of == date(2026, 8, 14)
    assert quote.close == Decimal("305.92999")


def test_the_same_real_bytes_refuse_the_unsettled_bar() -> None:
    """#535's selection rule, driven by real bytes: on the bar's own partition date the
    2026-08-14 row is potentially still forming, so the parser must resolve the prior
    settled session instead of it."""
    quote = parse_last_settled_close(_bytes(_TWELVE), partition=date(2026, 8, 14))
    assert quote is not None
    assert quote.as_of == date(2026, 8, 13)
    assert quote.close == Decimal("305.26001")


def test_the_two_origins_agree_within_the_declared_tolerance() -> None:
    """Cross-vendor fusion over real bytes: the exact comparison production ran.

    |305.93 - 305.92999| against the declared policy — the reconciliation that graded
    this cell ``agreed``, reproduced from the raw evidence instead of asserted from a
    fixture that agrees with itself by construction.
    """
    yahoo_bars = _parse_chart_response(json.loads(_bytes(_YAHOO), parse_float=Decimal))
    eligible = [b for b in yahoo_bars if b.date <= _PARTITION]
    assert eligible, "the cassette must contain bars at or before the capture partition"
    yahoo_close = max(eligible, key=lambda b: b.date).close
    twelve = parse_last_settled_close(_bytes(_TWELVE), partition=_PARTITION)
    assert twelve is not None
    tolerance = RECONCILIATION_POLICY.absolute_tolerance + RECONCILIATION_POLICY.relative_tolerance * max(
        abs(yahoo_close), abs(twelve.close)
    )
    assert abs(yahoo_close - twelve.close) <= tolerance


# -- the whole bar, not just the close --------------------------------------------------
#
# Both cassettes carry open/high/low/volume for the 2026-08-14 session. What the
# deployed parsers must now write for these exact bytes (Yahoo after float32
# recovery, Twelve Data verbatim):
#   yahoo      open 306.00  high 307.49     low 304.30     close 305.93     volume 28186700
#   twelvedata open 306     high 307.48999  low 304.29999  close 305.92999  volume 28186700


def _yahoo_quote():
    from data_engine.datahub.production_topt.market_price_adapter import quote_from_chart

    body = _bytes(_YAHOO)
    bars = _parse_chart_response(json.loads(body, parse_float=Decimal))
    return quote_from_chart(body, bars, cutoff=_PARTITION)


def test_yahoo_quote_carries_the_bar_the_production_bytes_encode() -> None:
    """The deployed fetcher's quote — the thing the adapter turns into a payload —
    carries the recovered bar, not only its close."""
    quote = _yahoo_quote()
    assert quote is not None and quote.as_of == date(2026, 8, 14)
    assert (quote.open, quote.high, quote.low, quote.close) == (
        Decimal("306.00"),
        Decimal("307.49"),
        Decimal("304.30"),
        Decimal("305.93"),
    )
    assert quote.volume == Decimal("28186700")
    assert quote.raw_bytes == _bytes(_YAHOO)


def test_twelve_data_settled_row_carries_the_bar_the_production_bytes_encode() -> None:
    quote = parse_last_settled_close(_bytes(_TWELVE), partition=_PARTITION)
    assert quote is not None and quote.as_of == date(2026, 8, 14)
    assert (quote.open, quote.high, quote.low, quote.close) == (
        Decimal("306"),
        Decimal("307.48999"),
        Decimal("304.29999"),
        Decimal("305.92999"),
    )
    assert quote.volume == Decimal("28186700")


def test_every_bar_field_agrees_within_its_declared_tolerance_over_real_bytes() -> None:
    """Per-field fusion over real bytes: each of the five fields under the policy that
    grades it in production. Volume is the field whose tolerance is not the price
    tolerance — consolidated-tape volumes are the vendor's own aggregation — so its
    observed spread on the settled bar is measured here, not assumed: it is zero."""
    from data_engine.datahub.quality_report import FIELD_RECONCILIATION_POLICIES, PRICE_BAR_FIELDS

    yahoo = _yahoo_quote()
    twelve = parse_last_settled_close(_bytes(_TWELVE), partition=_PARTITION)
    assert yahoo is not None and twelve is not None
    assert set(PRICE_BAR_FIELDS) == {"open", "high", "low", "close", "volume"}
    for field in PRICE_BAR_FIELDS:
        policy = FIELD_RECONCILIATION_POLICIES[field]
        left, right = getattr(yahoo, field), getattr(twelve, field)
        assert left is not None and right is not None, field
        tolerance = policy.absolute_tolerance + policy.relative_tolerance * max(abs(left), abs(right))
        assert abs(left - right) <= tolerance, f"{field}: {left} vs {right} exceeds {policy.policy_version}"
    assert yahoo.volume == twelve.volume, "the settled consolidated volumes are identical on these bytes"
