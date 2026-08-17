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
