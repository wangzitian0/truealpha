"""#1255: the Treasury source of the macro base data.

The fixtures are real bytes. On 2026-10-14 two files were fetched from the URL that `year_url`
builds. The 2026 file has 196 rows and 15,986 bytes (sha256 366158ae...2b2a9d). The 2023 file has
250 rows and 19,098 bytes (sha256 edb7f390...1249d4). Each fixture keeps the real header and the
first rows of the real file, byte for byte. The 2026 sample holds ten rows. It spans a weekend
(10/03-10/04) and a holiday weekend (Columbus Day, 10/10-10/12). No test here calls the Treasury.
On 2026-10-21 every row of both samples was compared with the live file: no cell differed. The
3 Mo, 2 Yr and 10 Yr columns of both whole files (1,350 values) were compared with the FRED series
DGS3MO, DGS2 and DGS10: no value differed.

The two headers differ: the 2026 file has the column "1.5 Month" and the 2023 file does not. So the
column "3 Mo" sits at index 4 in one file and at index 3 in the other. A parser that reads a
column by its position fails one of the two.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from data_engine import raw_store
from data_engine.datahub import treasury_rates
from data_engine.datahub.fetch_key import FetchKey
from data_engine.datahub.production_topt.source_registrations import (
    LEDGER_CAPACITIES,
    MACRO_REGISTRATIONS,
    MacroSeries,
    macro_registration_for,
)
from data_engine.datahub.risk_free import INSTRUMENT, MissingRiskFreeRate, risk_free_rate_at
from data_engine.datahub.treasury_rates import (
    REGISTRATION,
    Observation,
    TreasuryFetchError,
    TreasuryFormatError,
    TreasuryRefreshError,
    fetch_key_of,
    fetch_year,
    has_landed,
    knowable_at_of,
    parse_par_yield_csv,
    refresh_macro_series,
    year_url,
    years_to_fetch,
)
from data_engine.sources import gateway
from truealpha_contracts.models import DataSource
from truealpha_runtime.testing import InMemoryRawObjectStore, isolated_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from macro_seed import seed_observation  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "treasury"
SAMPLE_2026 = (FIXTURES / "daily_treasury_yield_curve_2026.sample.csv").read_bytes()
SAMPLE_2023 = (FIXTURES / "daily_treasury_yield_curve_2023.sample.csv").read_bytes()

THREE_MONTH, TWO_YEAR, TEN_YEAR = "us_treasury_bill_3_month", "us_treasury_note_2_year", "us_treasury_note_10_year"

#: The three columns of the ten rows of the 2026 sample, typed from the file. Percent, newest first.
PERCENT_2026 = {
    THREE_MONTH: {
        date(2026, 10, 13): "4.26",
        date(2026, 10, 9): "4.25",
        date(2026, 10, 8): "4.23",
        date(2026, 10, 7): "4.22",
        date(2026, 10, 6): "4.21",
        date(2026, 10, 5): "4.22",
        date(2026, 10, 2): "4.19",
        date(2026, 10, 1): "4.17",
        date(2026, 9, 30): "4.20",
        date(2026, 9, 29): "4.25",
    },
    TWO_YEAR: {
        date(2026, 10, 13): "4.79",
        date(2026, 10, 9): "4.80",
        date(2026, 10, 8): "4.75",
        date(2026, 10, 7): "4.77",
        date(2026, 10, 6): "4.79",
        date(2026, 10, 5): "4.84",
        date(2026, 10, 2): "4.83",
        date(2026, 10, 1): "4.78",
        date(2026, 9, 30): "4.88",
        date(2026, 9, 29): "4.89",
    },
    TEN_YEAR: {
        date(2026, 10, 13): "5.21",
        date(2026, 10, 9): "5.24",
        date(2026, 10, 8): "5.22",
        date(2026, 10, 7): "5.28",
        date(2026, 10, 6): "5.27",
        date(2026, 10, 5): "5.31",
        date(2026, 10, 2): "5.28",
        date(2026, 10, 1): "5.24",
        date(2026, 9, 30): "5.29",
        date(2026, 9, 29): "5.26",
    },
}
#: The three columns of the three rows of the 2023 sample. The "4 Mo" values are 5.41, 5.42 and 5.42.
PERCENT_2023 = {
    THREE_MONTH: {date(2023, 12, 29): "5.40", date(2023, 12, 28): "5.45", date(2023, 12, 27): "5.44"},
    TWO_YEAR: {date(2023, 12, 29): "4.23", date(2023, 12, 28): "4.26", date(2023, 12, 27): "4.20"},
    TEN_YEAR: {date(2023, 12, 29): "3.88", date(2023, 12, 28): "3.84", date(2023, 12, 27): "3.79"},
}


def _csv(*rows: str, header: str = 'Date,"3 Mo","2 Yr","10 Yr"') -> bytes:
    return "\n".join([header, *rows]).encode()


def _fractions(parsed: dict[str, tuple[Observation, ...]]) -> dict[str, dict[date, Decimal]]:
    return {sid: {item.observation_date: item.value for item in items} for sid, items in parsed.items()}


def _expected(percent: dict[str, dict[date, str]]) -> dict[str, dict[date, Decimal]]:
    return {sid: {day: Decimal(text) / 100 for day, text in days.items()} for sid, days in percent.items()}


# -- the fixtures are the real bytes -------------------------------------------------------


def test_the_fixtures_are_the_bytes_the_treasury_sent() -> None:
    """A hand edit of a fixture turns this red. The headers are the real ones."""
    assert hashlib.sha256(SAMPLE_2026).hexdigest() == "49c46cd7204faf974c6efaba49d1e23706213ce4e7881d77c198b8ee2f7c91d6"
    assert hashlib.sha256(SAMPLE_2023).hexdigest() == "c565db6b49b35188beaa7ee6ae89c03e73bad1cf70c8da71e3ddf1b22fb398ee"
    assert SAMPLE_2026.splitlines()[0] == (
        b'Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"'
    )
    assert SAMPLE_2023.splitlines()[0] == (
        b'Date,"1 Mo","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"'
    )
    assert len(SAMPLE_2026.splitlines()) == 11 and len(SAMPLE_2023.splitlines()) == 4


# -- registration ----------------------------------------------------------------------------


def test_the_registration_declares_the_seat_the_cost_the_axis_and_three_series() -> None:
    (registration,) = MACRO_REGISTRATIONS
    assert registration is REGISTRATION
    assert (registration.ledger_seat, registration.per_key_cost, registration.as_of_axis) == (
        "us-treasury",
        1,
        "business_day",
    )
    assert registration.capacity == LEDGER_CAPACITIES["us-treasury"], "the capacity comes from the seat, once"
    assert [(item.series_id, item.source_column) for item in registration.series] == [
        (THREE_MONTH, "3 Mo"),
        (TWO_YEAR, "2 Yr"),
        (TEN_YEAR, "10 Yr"),
    ]
    assert macro_registration_for(TWO_YEAR) is registration
    with pytest.raises(LookupError):
        macro_registration_for("us_cpi")


def test_the_seat_budget_covers_the_first_run_and_leaves_room_for_replays() -> None:
    """One request per year since `first_year`, in one run. Replays of the day add none (single fetch per key)."""
    first_run = date.today().year - REGISTRATION.first_year + 1
    budget = LEDGER_CAPACITIES["us-treasury"].daily_budget
    assert budget is not None
    assert first_run * REGISTRATION.per_key_cost <= budget // 2, "the first run spends at most half of the day"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"per_key_cost": 0}, "positive per_key_cost"),
        ({"as_of_axis": "settled_session"}, "as_of_axis"),
        ({"ledger_seat": "no-such-seat"}, "no capacity declaration"),
        ({"series": ()}, "at least one series"),
        ({"max_age_days": 0}, "max_age_days must be positive"),
        ({"series": (REGISTRATION.series[0], REGISTRATION.series[0])}, "appears twice"),
    ],
)
def test_a_macro_registration_that_breaks_a_rule_is_refused_at_import(change: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        dataclasses.replace(REGISTRATION, **change)


def test_a_series_needs_every_field() -> None:
    with pytest.raises(ValueError, match="source_column must not be empty"):
        MacroSeries(series_id="x", label="x", source_column=" ")


# -- parse -----------------------------------------------------------------------------------


def test_the_real_2026_sample_gives_each_series_as_decimal_fractions() -> None:
    parsed = parse_par_yield_csv(SAMPLE_2026, year=2026)

    assert _fractions(parsed) == _expected(PERCENT_2026)
    assert all([item.observation_date for item in items] == sorted(PERCENT_2026[sid]) for sid, items in parsed.items())
    newest = parsed[THREE_MONTH][-1]
    assert newest == Observation(date(2026, 10, 13), Decimal("0.0426"))
    assert str(newest.value) == "0.0426", "4.26 percent is 0.0426, not 4.26 and not 0.04259999"
    assert parsed[TWO_YEAR][-1].value == Decimal("0.0479") and parsed[TEN_YEAR][-1].value == Decimal("0.0521")
    assert all(isinstance(item.value, Decimal) for items in parsed.values() for item in items)


def test_each_column_is_found_by_its_name_in_both_headers() -> None:
    """Red on a parser that reads a fixed index: "3 Mo" sits at 4 in 2026 and at 3 in 2023, "2 Yr" at 8 and 7."""
    assert _fractions(parse_par_yield_csv(SAMPLE_2023, year=2023)) == _expected(PERCENT_2023)


def test_the_sample_keeps_the_gaps_of_the_real_calendar() -> None:
    """Weekend and federal holiday: the file has no row. The parser does not invent one."""
    for items in parse_par_yield_csv(SAMPLE_2026, year=2026).values():
        days = {item.observation_date for item in items}
        assert date(2026, 10, 12) not in days, "Columbus Day"
        assert date(2026, 10, 10) not in days and date(2026, 10, 11) not in days, "weekend"
        assert date(2026, 10, 3) not in days and date(2026, 10, 4) not in days, "weekend"
        assert (date(2026, 10, 13) - date(2026, 10, 9)).days == 4


@pytest.mark.parametrize(
    ("observation_date", "expected"),
    [
        (date(2026, 10, 13), datetime(2026, 10, 14, tzinfo=UTC)),
        (date(2026, 10, 9), datetime(2026, 10, 10, tzinfo=UTC)),
        (date(2026, 12, 31), datetime(2027, 1, 1, tzinfo=UTC)),
        (date(2024, 2, 28), datetime(2024, 2, 29, tzinfo=UTC)),
        (date(2023, 2, 28), datetime(2023, 3, 1, tzinfo=UTC)),
    ],
)
def test_a_rate_is_knowable_at_midnight_utc_of_the_next_day(observation_date: date, expected: datetime) -> None:
    assert knowable_at_of(observation_date) == expected
    assert knowable_at_of(observation_date) > datetime.combine(observation_date, datetime.max.time(), tzinfo=UTC)


def test_a_cell_without_a_value_is_skipped_for_that_series_only() -> None:
    body = _csv("10/13/2026,4.26,4.79,5.21", "10/09/2026,,4.80,5.24", "10/08/2026,4.23,4.75,")

    parsed = parse_par_yield_csv(body, year=2026)

    assert [item.observation_date for item in parsed[THREE_MONTH]] == [date(2026, 10, 8), date(2026, 10, 13)]
    assert [item.observation_date for item in parsed[TWO_YEAR]] == [
        date(2026, 10, 8),
        date(2026, 10, 9),
        date(2026, 10, 13),
    ]
    assert [item.observation_date for item in parsed[TEN_YEAR]] == [date(2026, 10, 9), date(2026, 10, 13)]


def test_a_header_only_file_is_a_file_without_observations() -> None:
    """The first days of a new year: the Treasury file exists and holds no row yet."""
    assert parse_par_yield_csv(_csv(), year=2027) == {THREE_MONTH: (), TWO_YEAR: (), TEN_YEAR: ()}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_csv("13/45/2026,4.26,4.79,5.21"), r"row 2: the date '13/45/2026' is not %m/%d/%Y"),
        (_csv("2026-10-13,4.26,4.79,5.21"), r"row 2: the date '2026-10-13' is not"),
        (_csv("10/13/2025,4.26,4.79,5.21"), r"row 2: the date 2025-10-13 is outside 2026"),
        (_csv("10/13/2026,abc,4.79,5.21"), r"row 2: the 3 Mo value 'abc' is not a decimal"),
        (_csv("10/13/2026,4.26,N/A,5.21"), r"row 2: the 2 Yr value 'N/A' is not a decimal"),
        (_csv("10/13/2026,4.26,4.79,NaN"), r"row 2: the 10 Yr value 'NaN' is outside 0 to 100 percent"),
        (_csv("10/13/2026,Infinity,4.79,5.21"), r"row 2: the 3 Mo value 'Infinity' is outside 0 to 100 percent"),
        (_csv("10/13/2026,-0.5,4.79,5.21"), r"row 2: the 3 Mo value '-0.5' is outside 0 to 100 percent"),
        (_csv("10/13/2026,4.26,100.5,5.21"), r"row 2: the 2 Yr value '100.5' is outside 0 to 100 percent"),
        (_csv("10/13/2026,4.26,4.79"), r"row 2: 3 fields, but the header has 4"),
        (_csv("10/13/2026,4.26,4.79,5.21,9.99"), r"row 2: 5 fields, but the header has 4"),
        (_csv("10/13/2026,4.26,4.79,5.21", "10/13/2026,4.27,4.80,5.22"), r"row 3: the date 2026-10-13 appears twice"),
        (_csv("10/13/2026,,4.79,5.21", "10/13/2026,4.27,4.80,5.22"), r"row 3: the date 2026-10-13 appears twice"),
        (_csv("10/13/2026,4.26,4.79", header='Date,"3 Mo","2 Yr"'), r"no \['10 Yr'\] header column"),
        (_csv("10/13/2026,4.26,5.21", header='Date,"3 Mo","10 Yr"'), r"no \['2 Yr'\] header column"),
        (_csv("10/13/2026,4.26,4.79,5.21", header='Day,"3 Mo","2 Yr","10 Yr"'), r"no \['Date'\] header column"),
        (b"", r"the 2026 file is empty"),
        (b"<html><body>Service unavailable</body></html>", r"header column"),
        (b"\xff\xfe\x00bad", r"is not UTF-8 text"),
    ],
    ids=[
        "date-not-a-date",
        "date-iso-format",
        "date-of-another-year",
        "value-text",
        "value-not-available",
        "value-nan",
        "value-infinity",
        "value-negative",
        "value-over-100",
        "row-too-short",
        "row-too-long",
        "date-twice",
        "date-twice-first-blank",
        "header-without-10-yr",
        "header-without-2-yr",
        "header-without-date",
        "empty-file",
        "html-page",
        "not-utf-8",
    ],
)
def test_a_malformed_file_is_refused_by_name(body: bytes, message: str) -> None:
    with pytest.raises(TreasuryFormatError, match=message):
        parse_par_yield_csv(body, year=2026)


def test_a_byte_order_mark_does_not_hide_the_date_column() -> None:
    assert len(parse_par_yield_csv(b"\xef\xbb\xbf" + _csv("10/13/2026,4.26,4.79,5.21"), year=2026)[THREE_MONTH]) == 1


# -- the fetch goes through the gateway --------------------------------------------------------


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body, self.status = body, status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _answer(monkeypatch: pytest.MonkeyPatch, body: bytes, status: int = 200) -> list[urllib.request.Request]:
    """The HTTP layer under `gateway.urlopen`: it records the requests and answers with `body`."""
    seen: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        seen.append(request)
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "answer", {}, io.BytesIO(b"unavailable"))  # type: ignore[arg-type]
        return _Response(body, status)

    monkeypatch.setattr(gateway.urllib.request, "urlopen", urlopen)
    return seen


def test_the_url_is_the_treasury_file_of_the_year() -> None:
    assert year_url(2026) == (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
        "/2026/all?type=daily_treasury_yield_curve&field_tdr_date_value=2026&page&_format=csv"
    )


def test_the_fetch_is_one_ledger_row_with_the_digest_of_the_bytes(monkeypatch, call_ledger) -> None:
    seen = _answer(monkeypatch, SAMPLE_2026)

    body = fetch_year(2026, _KEY)

    assert body == SAMPLE_2026
    (request,) = seen
    assert request.full_url == year_url(2026)
    assert request.get_header("User-agent") == treasury_rates.USER_AGENT
    (row,) = call_ledger
    assert (row.source, row.endpoint, row.status_code, row.ok) == (
        "us-treasury",
        "daily_treasury_yield_curve",
        200,
        True,
    )
    assert row.request_uri == gateway.redact_uri(year_url(2026)), "the ledger stores the normalized query"
    assert row.payload_sha256 == hashlib.sha256(SAMPLE_2026).hexdigest()
    assert row.byte_length == len(SAMPLE_2026)
    assert row.caller == "treasury_rates.fetch_year"
    assert row.fetch_key == _KEY.ledger_key, "the row names the key of the request"


def test_an_error_status_is_refused_by_name_and_recorded_as_a_failed_call(monkeypatch, call_ledger) -> None:
    _answer(monkeypatch, b"", status=503)

    with pytest.raises(TreasuryFetchError, match="HTTP 503 for the 2026 file"):
        fetch_year(2026, _KEY)

    (row,) = call_ledger
    assert (row.source, row.status_code, row.ok) == ("us-treasury", 503, False)


def test_the_seat_has_a_declared_capacity_and_the_raw_source_uses_its_name() -> None:
    assert DataSource.US_TREASURY.value == "us-treasury" == treasury_rates.SOURCE.value == REGISTRATION.ledger_seat
    seat = LEDGER_CAPACITIES["us-treasury"]
    assert seat.daily_budget is not None and seat.calls_per_window >= 1
    assert gateway.CAPACITIES["us-treasury"].enforceable


def test_a_spent_daily_budget_refuses_the_call_before_anything_is_sent(monkeypatch, call_ledger) -> None:
    seen = _answer(monkeypatch, SAMPLE_2026)
    now = datetime.now(UTC)
    for _ in range(LEDGER_CAPACITIES["us-treasury"].daily_budget or 0):
        call_ledger.append(
            gateway.CallRecord(source="us-treasury", endpoint="x", caller="earlier run", called_at=now, ok=True)
        )
    gate = gateway.CapacityGate(
        spent_since=call_ledger.calls_since, recent_calls=call_ledger.window, environment="production"
    )

    with gateway.capacity_scope(gate), pytest.raises(gateway.BudgetExhausted):
        fetch_year(2026, _KEY)

    assert seen == [], "the Treasury was never asked"


# -- the table ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    """One migrated database for the module. Each test empties the tables it uses (see `connection`)."""
    with isolated_test_database("macro") as url:
        yield url


@pytest.fixture
def connection(database: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database) as active:
        # TRUNCATE fires no row trigger, so it clears the append-only tables. CASCADE reaches the tables
        # that point to raw.fetches.
        active.execute("truncate mart.macro_series_daily, mart.macro_series, raw.fetches cascade")
        active.commit()
        yield active


def _rows(connection: psycopg.Connection, where: str = "true") -> list[tuple]:
    return connection.execute(
        f"select series_id, observation_date, value, fetch_id from mart.macro_series_daily where {where} "
        "order by series_id, observation_date, recorded_at, id"
    ).fetchall()


def _years(*bodies: tuple[int, bytes]):
    """A fetch function that answers with the given files and records the years it was asked for."""
    asked: list[int] = []
    files = dict(bodies)

    def fetch(year: int, _key: FetchKey) -> bytes:
        asked.append(year)
        return files[year]

    return fetch, asked


def _year_body(year: int, *days: str) -> bytes:
    return _csv(*[f"{day}/{year},4.50,4.60,4.70" for day in days])


def _all_years() -> dict[int, bytes]:
    return {
        2026: SAMPLE_2026,
        2025: _year_body(2025, "12/31", "12/30"),
        2024: _year_body(2024, "12/31", "12/30"),
        2023: SAMPLE_2023,
    }


#: A registration whose first year is 2023, so one first run asks for four files.
FOUR_YEARS = dataclasses.replace(REGISTRATION, first_year=2023)
NOW = datetime(2026, 10, 14, 21, 0, tzinfo=UTC)
TODAY = NOW.date()
_KEY = fetch_key_of(2026, on=TODAY)


def _refresh(connection, fetch, *, store=None, now: datetime = NOW, registration=FOUR_YEARS):
    return refresh_macro_series(
        connection,
        today=now.date(),
        fetch=fetch,
        clock=lambda: now,
        store=store or InMemoryRawObjectStore(),
        registration=registration,
    )


def test_the_first_run_fetches_four_years_and_stores_every_observation_of_every_series(connection) -> None:
    fetch, asked = _years(*_all_years().items())

    results = _refresh(connection, fetch)

    assert asked == [2026, 2025, 2024, 2023], "the current year first, then the earlier years"
    assert [(item.year, item.observations, item.inserted, item.reused) for item in results] == [
        (2026, 30, 30, False),
        (2025, 6, 6, False),
        (2024, 6, 6, False),
        (2023, 9, 9, False),
    ]
    assert len(_rows(connection)) == 51
    assert _rows(connection, f"series_id = '{THREE_MONTH}' and observation_date = '2026-10-13'")[0][:3] == (
        THREE_MONTH,
        date(2026, 10, 13),
        Decimal("0.0426"),
    )
    assert _rows(connection, f"series_id = '{TEN_YEAR}' and observation_date = '2026-10-13'")[0][2] == Decimal("0.0521")


def test_the_catalog_names_the_label_the_unit_and_the_cadence_of_each_series(connection) -> None:
    fetch, _ = _years(*_all_years().items())
    _refresh(connection, fetch)

    catalog = connection.execute(
        "select series_id, label, unit, source_id, max_age_days from mart.macro_series order by series_id"
    ).fetchall()

    assert catalog == sorted(
        (item.series_id, item.label, item.unit, REGISTRATION.source_id, REGISTRATION.max_age_days)
        for item in REGISTRATION.series
    )


def test_a_second_run_on_the_same_day_makes_no_request_and_inserts_nothing(connection) -> None:
    """Single fetch per key: the file of one year on one UTC day is fetched once."""
    fetch, asked = _years(*_all_years().items())
    store = InMemoryRawObjectStore()
    _refresh(connection, fetch, store=store)
    before = _rows(connection)
    asked.clear()

    results = _refresh(connection, fetch, store=store, now=NOW.replace(hour=22))

    assert asked == [], "no request: the key of 2026 on 2026-10-14 had landed"
    assert [(item.year, item.reused, item.fetch_id, item.inserted) for item in results] == [(2026, True, None, 0)]
    assert _rows(connection) == before


def test_a_run_on_the_next_day_fetches_the_current_year_again_and_only_that_year(connection) -> None:
    fetch, asked = _years(*_all_years().items())
    store = InMemoryRawObjectStore()
    _refresh(connection, fetch, store=store)
    before = _rows(connection)
    asked.clear()

    results = _refresh(connection, fetch, store=store, now=datetime(2026, 10, 15, 21, 0, tzinfo=UTC))

    assert asked == [2026], "the earlier years hold rows of every series already"
    assert [(item.year, item.observations, item.inserted) for item in results] == [(2026, 30, 0)]
    assert _rows(connection) == before, "no duplicate row"
    pointers = connection.execute(
        "select source_record_id, payload_sha256, object_uri from raw.fetches "
        "where source = 'us-treasury' and source_record_id like 'daily_treasury_yield_curve:year:2026:%' "
        "order by source_record_id"
    ).fetchall()
    assert [row[0].rsplit(":", 1)[1] for row in pointers] == ["2026-10-14", "2026-10-15"], "one pointer row per day"
    assert pointers[0][1:] == pointers[1][1:], "identical bytes: one digest and one object"


def test_the_key_of_a_file_is_the_year_and_the_utc_day() -> None:
    key = fetch_key_of(2026, on=date(2026, 10, 14))
    assert (key.seat, key.endpoint, key.canonical_entity_id, key.as_of) == (
        "us-treasury",
        "daily_treasury_yield_curve",
        "year:2026",
        "2026-10-14",
    )
    assert key.source_record_id == "daily_treasury_yield_curve:year:2026:2026-10-14"


def test_has_landed_is_true_for_the_day_of_the_fetch_only(connection) -> None:
    fetch, _ = _years(*_all_years().items())
    _refresh(connection, fetch)

    assert has_landed(connection, fetch_key_of(2026, on=date(2026, 10, 14)))
    assert not has_landed(connection, fetch_key_of(2026, on=date(2026, 10, 15)))
    assert not has_landed(connection, fetch_key_of(2022, on=date(2026, 10, 14)))


def test_a_new_day_appends_one_row_per_series_and_keeps_the_rest(connection) -> None:
    fetch, _ = _years(*_all_years().items())
    _refresh(connection, fetch)
    longer = SAMPLE_2026.replace(
        b"\n10/13/2026",
        b"\n10/14/2026,4.11,4.16,4.16,4.27,4.33,4.35,4.49,4.80,4.91,5.01,5.12,5.22,5.61,5.57\n10/13/2026",
    )
    fetch, _ = _years((2026, longer))

    (result,) = _refresh(connection, fetch, now=datetime(2026, 10, 15, 21, 0, tzinfo=UTC))

    assert (result.observations, result.inserted) == (33, 3)
    new = {row[0]: row[2] for row in _rows(connection, "observation_date = '2026-10-14'")}
    assert new == {THREE_MONTH: Decimal("0.0427"), TWO_YEAR: Decimal("0.0480"), TEN_YEAR: Decimal("0.0522")}
    assert len(_rows(connection)) == 54


def test_a_revised_value_is_a_new_vintage_of_that_series_only_and_the_old_one_stays(connection) -> None:
    fetch, _ = _years(*_all_years().items())
    store = InMemoryRawObjectStore()
    _refresh(connection, fetch, store=store)
    revised = SAMPLE_2026.replace(b"10/13/2026,4.10,4.15,4.15,4.26", b"10/13/2026,4.10,4.15,4.15,4.31")
    assert revised != SAMPLE_2026
    fetch, _ = _years((2026, revised))

    (result,) = _refresh(connection, fetch, store=store, now=datetime(2026, 10, 15, 21, 0, tzinfo=UTC))

    assert (result.observations, result.inserted) == (30, 1)
    assert [
        row[2] for row in _rows(connection, f"series_id = '{THREE_MONTH}' and observation_date = '2026-10-13'")
    ] == [
        Decimal("0.0426"),
        Decimal("0.0431"),
    ]
    assert len(_rows(connection, f"series_id = '{TWO_YEAR}' and observation_date = '2026-10-13'")) == 1
    knowable = [
        row[0]
        for row in connection.execute(
            "select knowable_at from mart.macro_series_daily where series_id = %s and observation_date = '2026-10-13' "
            "order by id",
            (THREE_MONTH,),
        ).fetchall()
    ]
    assert knowable == [datetime(2026, 10, 14, tzinfo=UTC), datetime(2026, 10, 15, 21, 0, tzinfo=UTC)], (
        "the first vintage is knowable from the source's cut, the revision from the moment it was fetched"
    )
    # Look-ahead check: a cutoff before the revision was fetched keeps the first vintage.
    assert risk_free_rate_at(connection, datetime(2026, 10, 14, 22, 15, tzinfo=UTC)).value == Decimal("0.0426")
    assert risk_free_rate_at(connection, datetime(2026, 10, 15, 22, 0, tzinfo=UTC)).value == Decimal("0.0431")


def test_a_value_that_returns_to_an_older_vintage_is_stored_again(connection) -> None:
    """The reader takes the newest vintage. If the Treasury goes back to the first value, that is news."""
    store = InMemoryRawObjectStore()
    revised = SAMPLE_2026.replace(b"10/13/2026,4.10,4.15,4.15,4.26", b"10/13/2026,4.10,4.15,4.15,4.31")
    for day, body in enumerate((SAMPLE_2026, revised, SAMPLE_2026), start=14):
        fetch, _ = _years((2026, body), *[(y, b) for y, b in _all_years().items() if y != 2026])
        _refresh(connection, fetch, store=store, now=datetime(2026, 10, day, 21, 0, tzinfo=UTC))

    assert [
        row[2] for row in _rows(connection, f"series_id = '{THREE_MONTH}' and observation_date = '2026-10-13'")
    ] == [
        Decimal("0.0426"),
        Decimal("0.0431"),
        Decimal("0.0426"),
    ]
    assert risk_free_rate_at(connection, datetime(2026, 10, 20, 22, 15, tzinfo=UTC)).value == Decimal("0.0426")
    assert risk_free_rate_at(connection, datetime(2026, 10, 15, 22, 0, tzinfo=UTC)).value == Decimal("0.0431")
    assert risk_free_rate_at(connection, datetime(2026, 10, 14, 22, 0, tzinfo=UTC)).value == Decimal("0.0426")


def test_each_row_says_where_and_when_it_came_from_and_points_to_the_bytes(connection) -> None:
    fetch, _ = _years(*_all_years().items())
    store = InMemoryRawObjectStore()
    _refresh(connection, fetch, store=store)

    row = connection.execute(
        "select m.fetch_id, m.source, m.fetched_at, f.source, f.source_record_id, f.payload_sha256, "
        "f.content_type, f.metadata, f.fetched_at "
        "from mart.macro_series_daily m join raw.fetches f on f.id = m.fetch_id "
        f"where m.series_id = '{TWO_YEAR}' and m.observation_date = '2026-10-13'"
    ).fetchone()
    fetch_id, source, fetched_at, raw_source, record_id, sha256, content_type, metadata, raw_fetched_at = row

    assert source == raw_source == "us-treasury"
    assert fetched_at == raw_fetched_at == NOW, "the row carries the fetch time of its bytes"
    assert (record_id, content_type) == ("daily_treasury_yield_curve:year:2026:2026-10-14", "text/csv")
    assert sha256 == hashlib.sha256(SAMPLE_2026).hexdigest()
    assert raw_store.get_payload(connection, fetch_id, store=store) == SAMPLE_2026, "the bytes dereference"
    assert metadata["year"] == 2026
    assert metadata["series"] == {THREE_MONTH: "3 Mo", TWO_YEAR: "2 Yr", TEN_YEAR: "10 Yr"}


def test_every_row_carries_a_confidence_and_the_next_midnight(connection) -> None:
    fetch, _ = _years(*_all_years().items())
    _refresh(connection, fetch)

    rows = connection.execute(
        "select series_id, observation_date, knowable_at, confidence, recorded_at from mart.macro_series_daily"
    ).fetchall()

    assert len(rows) == 51
    assert {row[0] for row in rows} == {THREE_MONTH, TWO_YEAR, TEN_YEAR}
    for _series, observation_date, knowable_at, confidence, recorded_at in rows:
        assert knowable_at == knowable_at_of(observation_date)
        assert confidence == treasury_rates.CONFIDENCE and 0 < confidence <= 1
        assert recorded_at.tzinfo is not None


def test_a_failed_year_rolls_back_alone_and_the_current_year_is_kept(connection) -> None:
    files = _all_years()
    files[2024] = b"<html>maintenance</html>"
    fetch, asked = _years(*files.items())

    with pytest.raises(
        TreasuryRefreshError, match=r"1 of 4 Treasury years failed: 2024: TreasuryFormatError"
    ) as raised:
        _refresh(connection, fetch)

    assert isinstance(raised.value.__cause__, TreasuryFormatError)
    assert asked == [2026, 2025, 2024, 2023], "a failed year does not stop the later years"
    assert {row[1].year for row in _rows(connection)} == {2026, 2025, 2023}


def test_a_year_that_fails_after_its_first_write_leaves_no_trace(connection, monkeypatch) -> None:
    """The raw row of the year is written before the observations. A failure in between rolls both back,
    or the next year's commit would keep a raw row that no observation points to. A raw row without
    observations would also make the key look landed, and the replay of the day would skip the year."""
    real = treasury_rates.persist_observations

    def failing(connection, observations, *, year, fetch_id, fetched_at):
        if year == 2024:
            raise psycopg.errors.DataError("the write of 2024 failed")
        return real(connection, observations, year=year, fetch_id=fetch_id, fetched_at=fetched_at)

    monkeypatch.setattr(treasury_rates, "persist_observations", failing)
    fetch, _ = _years(*_all_years().items())

    with pytest.raises(TreasuryRefreshError, match="1 of 4 Treasury years failed: 2024: DataError"):
        _refresh(connection, fetch)

    records = connection.execute(
        "select source_record_id from raw.fetches where source = 'us-treasury' order by 1"
    ).fetchall()
    assert records == [
        ("daily_treasury_yield_curve:year:2023:2026-10-14",),
        ("daily_treasury_yield_curve:year:2025:2026-10-14",),
        ("daily_treasury_yield_curve:year:2026:2026-10-14",),
    ], "no raw row of the failed year"
    assert {row[1].year for row in _rows(connection)} == {2026, 2025, 2023}


def test_a_failed_fetch_of_the_current_year_still_lets_the_backfill_run_and_then_fails_the_run(connection) -> None:
    files = _all_years()

    def fetch(year: int, _key: FetchKey) -> bytes:
        if year == 2026:
            raise TreasuryFetchError("the Treasury answered HTTP 503 for the 2026 file")
        return files[year]

    with pytest.raises(TreasuryRefreshError, match="1 of 4 Treasury years failed: 2026: TreasuryFetchError"):
        _refresh(connection, fetch)

    assert {row[1].year for row in _rows(connection)} == {2025, 2024, 2023}


def test_two_writers_take_turns_on_the_advisory_lock(database) -> None:
    """Red when the lock is dropped: the check against the stored vintage and the insert would race."""
    with psycopg.connect(database) as holder, psycopg.connect(database) as writer:
        holder.execute("truncate mart.macro_series_daily, mart.macro_series, raw.fetches cascade")
        holder.commit()
        treasury_rates.register_series(writer)
        fetch_id = seed_observation(writer, date(2026, 10, 1))
        writer.commit()
        holder.execute("select pg_advisory_xact_lock(hashtext(%s))", (treasury_rates._LOCK_NAME,))
        writer.execute("set lock_timeout = '300ms'")
        observations = {THREE_MONTH: (Observation(date(2026, 10, 13), Decimal("0.0426")),)}

        with pytest.raises(psycopg.errors.LockNotAvailable):
            treasury_rates.persist_observations(writer, observations, year=2026, fetch_id=fetch_id, fetched_at=NOW)
        writer.rollback()

        holder.commit()  # the first writer is done: the lock is free
        writer.execute("set lock_timeout = '300ms'")
        assert (
            treasury_rates.persist_observations(writer, observations, year=2026, fetch_id=fetch_id, fetched_at=NOW) == 1
        )


# -- which years a run asks for -------------------------------------------------------------------


def _store_years(connection, *years: int, series: tuple[str, ...] = (THREE_MONTH, TWO_YEAR, TEN_YEAR)) -> None:
    for year in years:
        for series_id in series:
            seed_observation(connection, date(year, 12, 29), "0.04", series_id=series_id)


@pytest.mark.parametrize(
    ("today", "stored", "expected"),
    [
        (date(2026, 10, 14), (), [2026, 2025, 2024, 2023]),
        (date(2026, 10, 14), (2023, 2024, 2025, 2026), [2026]),
        (date(2026, 10, 14), (2023, 2025), [2026, 2024]),
        (date(2026, 10, 14), (2022,), [2026, 2025, 2024, 2023]),
        (date(2027, 1, 5), (2023, 2024, 2025, 2026), [2027, 2026]),
        (date(2027, 1, 5), (), [2027, 2026, 2025, 2024, 2023]),
        (date(2027, 2, 1), (2023, 2024, 2025, 2026), [2027]),
    ],
    ids=["empty", "complete", "one-hole", "only-too-old", "january-refetches-last-year", "january-empty", "february"],
)
def test_the_years_of_one_run(connection, today: date, stored: tuple[int, ...], expected: list[int]) -> None:
    _store_years(connection, *stored)

    assert years_to_fetch(connection, today=today, registration=FOUR_YEARS) == expected


def test_a_year_that_lacks_one_series_is_fetched_again(connection) -> None:
    """A series added to the registration later has no history. Its years must be loaded too."""
    _store_years(connection, 2023, 2024, 2025, 2026, series=(THREE_MONTH, TWO_YEAR))

    assert years_to_fetch(connection, today=TODAY, registration=FOUR_YEARS) == [2026, 2025, 2024, 2023]


@pytest.mark.parametrize(
    ("last_day", "fetched_again"),
    [
        (date(2024, 11, 29), True),
        (date(2024, 12, 24), True),
        (date(2024, 12, 28), True),
        (date(2024, 12, 29), False),
        (date(2024, 12, 31), False),
    ],
    ids=["november", "24-december", "28-december", "29-december", "31-december"],
)
def test_a_year_that_stops_before_29_december_is_fetched_again(connection, last_day: date, fetched_again: bool) -> None:
    """Runs that failed across the turn of the year leave a hole at its end. Nothing else would fill it.

    The boundary is `COMPLETE_FROM`: a change to 24 or 28 December turns the 28 December row green."""
    _store_years(connection, 2023, 2025, 2026)
    for series_id in (THREE_MONTH, TWO_YEAR, TEN_YEAR):
        seed_observation(connection, last_day, "0.04", series_id=series_id)

    expected = [2026, 2024] if fetched_again else [2026]
    assert years_to_fetch(connection, today=TODAY, registration=FOUR_YEARS) == expected
    assert treasury_rates.COMPLETE_FROM == (12, 29)


def test_the_first_run_of_the_registration_asks_for_each_year_since_its_first_year(connection) -> None:
    years = years_to_fetch(connection, today=date(2026, 10, 14))

    assert years == list(range(2026, REGISTRATION.first_year - 1, -1))
    assert years[0] == 2026 and years[-1] == REGISTRATION.first_year == 2000


# -- the table itself -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    ["update mart.macro_series_daily set value = 0.01", "delete from mart.macro_series_daily"],
    ids=["update", "delete"],
)
def test_the_table_refuses_update_and_delete(connection, statement: str) -> None:
    seed_observation(connection, date(2026, 10, 13))

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        connection.execute(statement)


_INSERT_ROW = (
    "insert into mart.macro_series_daily "
    "(series_id, observation_date, value, knowable_at, source, fetch_id, fetched_at, confidence) "
    "values (%s, '2026-10-13', %s, %s, 'us-treasury', %s, now(), %s)"
)


@pytest.mark.parametrize(
    ("knowable_at", "accepted"),
    [
        (datetime(2026, 10, 14, 0, 0, tzinfo=UTC), True),
        (datetime(2026, 10, 14, 0, 0, 1, tzinfo=UTC), True),
        (datetime(2026, 10, 13, 0, 0, tzinfo=UTC), False),
        (datetime(2026, 10, 13, 23, 59, 59, tzinfo=UTC), False),
    ],
    ids=["next-midnight", "after-next-midnight", "own-midnight", "own-day-end"],
)
def test_a_value_is_never_known_on_its_own_day(connection, knowable_at: datetime, accepted: bool) -> None:
    """Red when the CHECK is dropped: a row that is knowable on its own day would reach a backtest."""
    fetch_id = seed_observation(connection, date(2026, 10, 1))
    args = (INSTRUMENT, Decimal("0.0426"), knowable_at, fetch_id, Decimal("0.92"))
    if accepted:
        connection.execute(_INSERT_ROW, args)
    else:
        with pytest.raises(psycopg.errors.CheckViolation, match="knowable_after_the_day"):
            connection.execute(_INSERT_ROW, args)


@pytest.mark.parametrize(
    ("value", "confidence"), [("NaN", "0.9"), ("0.04", "-0.1"), ("0.04", "1.1")], ids=["nan", "low", "high"]
)
def test_the_table_bounds_the_value_and_the_confidence(connection, value: str, confidence: str) -> None:
    fetch_id = seed_observation(connection, date(2026, 10, 1))

    with pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            _INSERT_ROW, (INSTRUMENT, Decimal(value), datetime(2026, 10, 14, tzinfo=UTC), fetch_id, Decimal(confidence))
        )


def test_a_row_must_point_to_a_stored_fetch_and_a_cataloged_series(connection) -> None:
    fetch_id = seed_observation(connection, date(2026, 10, 1))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        connection.execute(
            _INSERT_ROW,
            (INSTRUMENT, Decimal("0.04"), datetime(2026, 10, 14, tzinfo=UTC), 2_000_000_000, Decimal("0.9")),
        )
    connection.rollback()
    fetch_id = seed_observation(connection, date(2026, 10, 1))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        connection.execute(
            _INSERT_ROW, ("us_cpi", Decimal("0.04"), datetime(2026, 10, 14, tzinfo=UTC), fetch_id, Decimal("0.9"))
        )


def test_the_pointer_check_fails_loudly_on_a_column_name_that_does_not_exist(connection) -> None:
    """A mistyped column in the trigger arguments would read as NULL and pass every insert."""
    connection.execute("create temp table pointer_probe (id bigint)")
    connection.execute(
        "create trigger probe before insert on pointer_probe for each row "
        "execute function mart.require_stored_fetch('no_such_column')"
    )

    with pytest.raises(psycopg.errors.RaiseException, match="no_such_column is not a column"):
        connection.execute("insert into pointer_probe values (1)")


def test_the_pointer_check_fails_loudly_on_a_trigger_without_an_argument(connection) -> None:
    """With no argument `tg_argv[0]` is NULL and the check would be skipped for every insert."""
    connection.execute("create temp table pointer_probe_bare (id bigint)")
    connection.execute(
        "create trigger probe before insert on pointer_probe_bare for each row "
        "execute function mart.require_stored_fetch()"
    )

    with pytest.raises(psycopg.errors.RaiseException, match="takes exactly one argument"):
        connection.execute("insert into pointer_probe_bare values (1)")


def test_the_pointer_check_fails_loudly_on_a_trigger_with_two_arguments(connection) -> None:
    """Two arguments are not a column name: the message says so, not that the first column is unknown."""
    connection.execute("create temp table pointer_probe_two (id bigint)")
    connection.execute(
        "create trigger probe before insert on pointer_probe_two for each row "
        "execute function mart.require_stored_fetch('id', 'extra')"
    )

    with pytest.raises(psycopg.errors.RaiseException, match="takes exactly one argument"):
        connection.execute("insert into pointer_probe_two values (1)")


def test_the_fetch_time_is_read_after_the_answer_arrives(connection) -> None:
    """A revised value is knowable from the moment the answer arrived, not from the moment the request began.

    The request itself takes five seconds of the test clock. Red when the stored time is read before
    `fetch()`: a revision would then reach a cutoff five seconds early."""
    one_year = dataclasses.replace(REGISTRATION, first_year=2026)
    store = InMemoryRawObjectStore()
    first_fetch, _ = _years((2026, SAMPLE_2026))
    refresh_macro_series(
        connection, today=TODAY, fetch=first_fetch, clock=lambda: NOW, store=store, registration=one_year
    )
    revised = SAMPLE_2026.replace(b"10/13/2026,4.10,4.15,4.15,4.26", b"10/13/2026,4.10,4.15,4.15,4.31")
    started = datetime(2026, 10, 15, 21, 0, tzinfo=UTC)
    answered = started + timedelta(seconds=5)
    now = {"value": started}

    def slow_fetch(year: int, _key: FetchKey) -> bytes:
        now["value"] += timedelta(seconds=5)
        return revised

    refresh_macro_series(
        connection,
        today=date(2026, 10, 15),
        fetch=slow_fetch,
        clock=lambda: now["value"],
        store=store,
        registration=one_year,
    )

    row = connection.execute(
        "select knowable_at, fetched_at from mart.macro_series_daily "
        f"where series_id = '{THREE_MONTH}' and observation_date = '2026-10-13' order by id desc limit 1"
    ).fetchone()
    assert row == (answered, answered)
    assert connection.execute(
        "select fetched_at from raw.fetches where source_record_id like '%year:2026:2026-10-15'"
    ).fetchone() == (answered,)


def test_an_empty_table_has_no_rate_for_any_cutoff(connection) -> None:
    with pytest.raises(MissingRiskFreeRate, match="none stored"):
        risk_free_rate_at(connection, datetime(2026, 10, 14, 22, 15, tzinfo=UTC))
