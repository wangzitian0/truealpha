"""#1255: the Treasury source of the risk-free rate.

The fixtures are real bytes. On 2026-10-14 two files were fetched from the URL that `year_url`
builds. The 2026 file has 196 rows and 15,986 bytes (sha256 366158ae...2b2a9d). The 2023 file has
250 rows and 19,098 bytes (sha256 edb7f390...1249d4). Each fixture keeps the real header and the
first rows of the real file, byte for byte. The 2026 sample holds ten rows. It spans a weekend
(10/03-10/04) and a holiday weekend (Columbus Day, 10/10-10/12). No test here calls the Treasury.

The two headers differ: the 2026 file has the column "1.5 Month" and the 2023 file does not. So the
column "3 Mo" sits at index 4 in one file and at index 3 in the other. A parser that reads a
column by its position fails one of the two.
"""

from __future__ import annotations

import hashlib
import io
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from data_engine import raw_store
from data_engine.datahub import treasury_rates
from data_engine.datahub.production_topt.source_registrations import LEDGER_CAPACITIES
from data_engine.datahub.risk_free import INSTRUMENT, MissingRiskFreeRate, risk_free_rate_at
from data_engine.datahub.treasury_rates import (
    Observation,
    TreasuryFetchError,
    TreasuryFormatError,
    TreasuryRefreshError,
    fetch_year,
    knowable_at_of,
    parse_par_yield_csv,
    refresh_risk_free_rates,
    year_url,
    years_to_fetch,
)
from data_engine.sources import gateway
from truealpha_contracts.models import DataSource
from truealpha_runtime.testing import InMemoryRawObjectStore, isolated_test_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "support"))
from risk_free_seed import seed_risk_free_rate  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "treasury"
SAMPLE_2026 = (FIXTURES / "daily_treasury_yield_curve_2026.sample.csv").read_bytes()
SAMPLE_2023 = (FIXTURES / "daily_treasury_yield_curve_2023.sample.csv").read_bytes()

#: The "3 Mo" column of the ten rows of the 2026 sample, typed from the file. Percent, newest first.
PERCENT_2026 = {
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
}
#: The "3 Mo" column of the three rows of the 2023 sample. The "4 Mo" values are 5.41, 5.42 and 5.42.
PERCENT_2023 = {date(2023, 12, 29): "5.40", date(2023, 12, 28): "5.45", date(2023, 12, 27): "5.44"}


def _csv(*rows: str, header: str = 'Date,"1 Mo","3 Mo","6 Mo"') -> bytes:
    return "\n".join([header, *rows]).encode()


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


# -- parse -----------------------------------------------------------------------------------


def test_the_real_2026_sample_gives_the_three_month_column_as_decimal_fractions() -> None:
    observations = parse_par_yield_csv(SAMPLE_2026, year=2026)

    assert {item.observation_date: item.annualized_rate for item in observations} == {
        day: Decimal(percent) / 100 for day, percent in PERCENT_2026.items()
    }
    assert [item.observation_date for item in observations] == sorted(PERCENT_2026), "oldest first"
    newest = observations[-1]
    assert newest == Observation(date(2026, 10, 13), Decimal("0.0426"))
    assert str(newest.annualized_rate) == "0.0426", "4.26 percent is 0.0426, not 4.26 and not 0.04259999"
    assert all(isinstance(item.annualized_rate, Decimal) for item in observations)


def test_the_three_month_column_is_found_by_its_name_in_both_headers() -> None:
    """Red on a parser that reads index 4 (the 2026 position of "3 Mo"): the 2023 file would give "4 Mo"."""
    observations = parse_par_yield_csv(SAMPLE_2023, year=2023)

    assert {item.observation_date: item.annualized_rate for item in observations} == {
        day: Decimal(percent) / 100 for day, percent in PERCENT_2023.items()
    }


def test_the_sample_keeps_the_gaps_of_the_real_calendar() -> None:
    """Weekend and federal holiday: the file has no row. The parser does not invent one."""
    days = {item.observation_date for item in parse_par_yield_csv(SAMPLE_2026, year=2026)}

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


def test_rows_without_a_three_month_value_are_skipped() -> None:
    body = _csv("10/13/2026,4.10,4.26,4.34", "10/09/2026,4.13,,4.32", "10/08/2026,4.14,4.23,4.30")

    assert [item.observation_date for item in parse_par_yield_csv(body, year=2026)] == [
        date(2026, 10, 8),
        date(2026, 10, 13),
    ]


def test_a_header_only_file_is_a_file_without_observations() -> None:
    """The first days of a new year: the Treasury file exists and holds no row yet."""
    assert parse_par_yield_csv(_csv(), year=2027) == ()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_csv("13/45/2026,4.10,4.26,4.34"), r"row 2: the date '13/45/2026' is not %m/%d/%Y"),
        (_csv("2026-10-13,4.10,4.26,4.34"), r"row 2: the date '2026-10-13' is not"),
        (_csv("10/13/2025,4.10,4.26,4.34"), r"row 2: the date 2025-10-13 is outside 2026"),
        (_csv("10/13/2026,4.10,abc,4.34"), r"row 2: the 3 Mo value 'abc' is not a decimal"),
        (_csv("10/13/2026,4.10,N/A,4.34"), r"row 2: the 3 Mo value 'N/A' is not a decimal"),
        (_csv("10/13/2026,4.10,NaN,4.34"), r"row 2: the 3 Mo value 'NaN' is outside 0 to 100 percent"),
        (_csv("10/13/2026,4.10,Infinity,4.34"), r"row 2: the 3 Mo value 'Infinity' is outside 0 to 100 percent"),
        (_csv("10/13/2026,4.10,-0.5,4.34"), r"row 2: the 3 Mo value '-0.5' is outside 0 to 100 percent"),
        (_csv("10/13/2026,4.10,100.5,4.34"), r"row 2: the 3 Mo value '100.5' is outside 0 to 100 percent"),
        (_csv("10/13/2026,4.10,4.26"), r"row 2: 3 fields, but the header has 4"),
        (_csv("10/13/2026,4.10,4.26,4.34,9.99"), r"row 2: 5 fields, but the header has 4"),
        (_csv("10/13/2026,4.10,4.26,4.34", "10/13/2026,4.11,4.27,4.35"), r"row 3: the date 2026-10-13 appears twice"),
        (_csv("10/13/2026,4.10,,4.34", "10/13/2026,4.11,4.27,4.35"), r"row 3: the date 2026-10-13 appears twice"),
        (_csv("10/13/2026,4.10,4.26,4.34", header='Date,"1 Mo","6 Mo"'), r"no 'Date' and '3 Mo' header"),
        (_csv("10/13/2026,4.10,4.26,4.34", header='Day,"1 Mo","3 Mo","6 Mo"'), r"no 'Date' and '3 Mo' header"),
        (b"", r"the 2026 file is empty"),
        (b"<html><body>Service unavailable</body></html>", r"no 'Date' and '3 Mo' header"),
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
        "header-without-3-mo",
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
    assert len(parse_par_yield_csv(b"\xef\xbb\xbf" + _csv("10/13/2026,4.10,4.26,4.34"), year=2026)) == 1


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

    body = fetch_year(2026)

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


def test_an_error_status_is_refused_by_name_and_recorded_as_a_failed_call(monkeypatch, call_ledger) -> None:
    _answer(monkeypatch, b"", status=503)

    with pytest.raises(TreasuryFetchError, match="HTTP 503 for the 2026 file"):
        fetch_year(2026)

    (row,) = call_ledger
    assert (row.source, row.status_code, row.ok) == ("us-treasury", 503, False)


def test_the_seat_has_a_declared_capacity_and_the_raw_source_uses_its_name() -> None:
    assert DataSource.US_TREASURY.value == "us-treasury" == treasury_rates.SOURCE.value
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
        fetch_year(2026)

    assert seen == [], "the Treasury was never asked"


# -- the table ---------------------------------------------------------------------------------


@pytest.fixture
def database() -> Iterator[str]:
    with isolated_test_database("treasury") as url:
        yield url


@pytest.fixture
def connection(database: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database) as active:
        yield active


def _rows(connection: psycopg.Connection, where: str = "true") -> list[tuple]:
    return connection.execute(
        f"select observation_date, annualized_rate, fetch_id from staging.risk_free_rates_daily where {where} "
        "order by observation_date, recorded_at, id"
    ).fetchall()


def _years(*bodies: tuple[int, bytes]):
    """A fetch function that answers with the given files and records the years it was asked for."""
    asked: list[int] = []
    files = dict(bodies)

    def fetch(year: int) -> bytes:
        asked.append(year)
        return files[year]

    return fetch, asked


def _year_body(year: int, *days: str) -> bytes:
    return _csv(*[f"{day}/{year},4.00,4.50,4.60" for day in days])


def _all_years() -> dict[int, bytes]:
    return {
        2026: SAMPLE_2026,
        2025: _year_body(2025, "12/31", "12/30"),
        2024: _year_body(2024, "12/31", "12/30"),
        2023: SAMPLE_2023,
    }


TODAY = date(2026, 10, 14)


def test_the_first_run_fetches_four_years_and_stores_every_observation(connection) -> None:
    fetch, asked = _years(*_all_years().items())

    results = refresh_risk_free_rates(connection, today=TODAY, fetch=fetch, store=InMemoryRawObjectStore())

    assert asked == [2026, 2025, 2024, 2023], "the current year first, then the earlier years"
    assert [(item.year, item.observations, item.inserted) for item in results] == [
        (2026, 10, 10),
        (2025, 2, 2),
        (2024, 2, 2),
        (2023, 3, 3),
    ]
    assert len(_rows(connection)) == 17
    assert (date(2026, 10, 13), Decimal("0.0426")) == _rows(connection, "observation_date = '2026-10-13'")[0][:2]


def test_a_second_run_inserts_nothing_and_asks_only_for_the_current_year(connection) -> None:
    fetch, asked = _years(*_all_years().items())
    store = InMemoryRawObjectStore()
    refresh_risk_free_rates(connection, today=TODAY, fetch=fetch, store=store)
    before = _rows(connection)
    fetches_before = connection.execute("select count(*) from raw.fetches where source = 'us-treasury'").fetchone()

    asked.clear()
    results = refresh_risk_free_rates(connection, today=TODAY, fetch=fetch, store=store)

    assert asked == [2026], "the earlier years hold rows already"
    assert [(item.year, item.observations, item.inserted) for item in results] == [(2026, 10, 0)]
    assert _rows(connection) == before, "no duplicate row"
    assert (
        connection.execute("select count(*) from raw.fetches where source = 'us-treasury'").fetchone() == fetches_before
    ), "identical bytes collapse onto the stored fetch row"


def test_a_new_day_appends_one_row_and_keeps_the_rest(connection) -> None:
    fetch, _ = _years(*_all_years().items())
    refresh_risk_free_rates(connection, today=TODAY, fetch=fetch, store=InMemoryRawObjectStore())
    longer = SAMPLE_2026.replace(
        b"\n10/13/2026",
        b"\n10/14/2026,4.11,4.16,4.16,4.27,4.33,4.35,4.49,4.80,4.91,5.01,5.12,5.22,5.61,5.57\n10/13/2026",
    )
    fetch, _ = _years((2026, longer))

    (result,) = refresh_risk_free_rates(
        connection, today=date(2026, 10, 15), fetch=fetch, store=InMemoryRawObjectStore()
    )

    assert (result.observations, result.inserted) == (11, 1)
    assert _rows(connection, "observation_date = '2026-10-14'")[0][:2] == (date(2026, 10, 14), Decimal("0.0427"))
    assert len(_rows(connection)) == 18


def test_a_revised_value_is_a_new_vintage_and_the_old_one_stays(connection) -> None:
    fetch, _ = _years(*_all_years().items())
    store = InMemoryRawObjectStore()
    refresh_risk_free_rates(connection, today=TODAY, fetch=fetch, store=store)
    revised = SAMPLE_2026.replace(b"10/13/2026,4.10,4.15,4.15,4.26", b"10/13/2026,4.10,4.15,4.15,4.31")
    assert revised != SAMPLE_2026
    fetch, _ = _years((2026, revised))

    (result,) = refresh_risk_free_rates(connection, today=TODAY, fetch=fetch, store=store)

    assert (result.observations, result.inserted) == (10, 1)
    assert [row[1] for row in _rows(connection, "observation_date = '2026-10-13'")] == [
        Decimal("0.0426"),
        Decimal("0.0431"),
    ]
    assert risk_free_rate_at(connection, datetime(2026, 10, 14, 22, 15, tzinfo=UTC)).value == Decimal("0.0431")


def test_a_value_that_returns_to_an_older_vintage_is_stored_again(connection) -> None:
    """The reader takes the newest vintage. If the Treasury goes back to the first value, that is news."""
    store = InMemoryRawObjectStore()
    original = SAMPLE_2026
    revised = SAMPLE_2026.replace(b"10/13/2026,4.10,4.15,4.15,4.26", b"10/13/2026,4.10,4.15,4.15,4.31")
    for body in (original, revised, original):
        fetch, _ = _years((2026, body), *[(y, b) for y, b in _all_years().items() if y != 2026])
        refresh_risk_free_rates(connection, today=TODAY, fetch=fetch, store=store)

    assert [row[1] for row in _rows(connection, "observation_date = '2026-10-13'")] == [
        Decimal("0.0426"),
        Decimal("0.0431"),
        Decimal("0.0426"),
    ]
    assert risk_free_rate_at(connection, datetime(2026, 10, 14, 22, 15, tzinfo=UTC)).value == Decimal("0.0426")


def test_each_row_points_to_the_stored_bytes_that_carried_its_value(connection) -> None:
    fetch, _ = _years(*_all_years().items())
    store = InMemoryRawObjectStore()
    refresh_risk_free_rates(connection, today=TODAY, fetch=fetch, store=store)

    row = connection.execute(
        "select r.fetch_id, f.source, f.source_record_id, f.payload_sha256, f.content_type, f.metadata "
        "from staging.risk_free_rates_daily r join raw.fetches f on f.id = r.fetch_id "
        "where r.observation_date = '2026-10-13'"
    ).fetchone()
    fetch_id, source, record_id, sha256, content_type, metadata = row

    assert (source, record_id, content_type) == ("us-treasury", "daily_treasury_yield_curve:2026", "text/csv")
    assert sha256 == hashlib.sha256(SAMPLE_2026).hexdigest()
    assert raw_store.get_payload(connection, fetch_id, store=store) == SAMPLE_2026, "the bytes dereference"
    assert metadata["instrument"] == INSTRUMENT and metadata["column"] == "3 Mo" and metadata["year"] == 2026


def test_every_row_carries_the_instrument_a_confidence_and_the_next_midnight(connection) -> None:
    fetch, _ = _years(*_all_years().items())
    refresh_risk_free_rates(connection, today=TODAY, fetch=fetch, store=InMemoryRawObjectStore())

    rows = connection.execute(
        "select instrument, observation_date, knowable_at, confidence, recorded_at from staging.risk_free_rates_daily"
    ).fetchall()

    assert len(rows) == 17
    for instrument, observation_date, knowable_at, confidence, recorded_at in rows:
        assert instrument == INSTRUMENT
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
        refresh_risk_free_rates(connection, today=TODAY, fetch=fetch, store=InMemoryRawObjectStore())

    assert isinstance(raised.value.__cause__, TreasuryFormatError)
    assert asked == [2026, 2025, 2024, 2023], "a failed year does not stop the later years"
    assert {row[0].year for row in _rows(connection)} == {2026, 2025, 2023}


def test_a_year_that_fails_after_its_first_write_leaves_no_trace(connection, monkeypatch) -> None:
    """The raw row of the year is written before the observations. A failure in between rolls both back,
    or the next year's commit would keep a raw row that no observation points to."""
    real = treasury_rates.persist_observations

    def failing(connection, observations, *, year, fetch_id):
        if year == 2024:
            raise psycopg.errors.DataError("the write of 2024 failed")
        return real(connection, observations, year=year, fetch_id=fetch_id)

    monkeypatch.setattr(treasury_rates, "persist_observations", failing)
    fetch, _ = _years(*_all_years().items())

    with pytest.raises(TreasuryRefreshError, match="1 of 4 Treasury years failed: 2024: DataError"):
        refresh_risk_free_rates(connection, today=TODAY, fetch=fetch, store=InMemoryRawObjectStore())

    records = connection.execute(
        "select source_record_id from raw.fetches where source = 'us-treasury' order by 1"
    ).fetchall()
    assert records == [
        ("daily_treasury_yield_curve:2023",),
        ("daily_treasury_yield_curve:2025",),
        ("daily_treasury_yield_curve:2026",),
    ], "no raw row of the failed year"
    assert {row[0].year for row in _rows(connection)} == {2026, 2025, 2023}


def test_a_failed_fetch_of_the_current_year_still_lets_the_backfill_run_and_then_fails_the_run(connection) -> None:
    files = _all_years()

    def fetch(year: int) -> bytes:
        if year == 2026:
            raise TreasuryFetchError("the Treasury answered HTTP 503 for the 2026 file")
        return files[year]

    with pytest.raises(TreasuryRefreshError, match="1 of 4 Treasury years failed: 2026: TreasuryFetchError"):
        refresh_risk_free_rates(connection, today=TODAY, fetch=fetch, store=InMemoryRawObjectStore())

    assert {row[0].year for row in _rows(connection)} == {2025, 2024, 2023}


# -- which years a run asks for -------------------------------------------------------------------


def _store_years(connection, *years: int) -> None:
    for year in years:
        seed_risk_free_rate(connection, date(year, 6, 1))


@pytest.mark.parametrize(
    ("today", "stored", "expected"),
    [
        (date(2026, 10, 14), (), [2026, 2025, 2024, 2023]),
        (date(2026, 10, 14), (2023, 2024, 2025, 2026), [2026]),
        (date(2026, 10, 14), (2023, 2025), [2026, 2024]),
        (date(2026, 10, 14), (2022,), [2026, 2025, 2024, 2023]),
        (date(2027, 1, 5), (2024, 2025, 2026), [2027, 2026]),
        (date(2027, 1, 5), (), [2027, 2026, 2025, 2024]),
        (date(2027, 2, 1), (2024, 2025, 2026), [2027]),
    ],
    ids=["empty", "complete", "one-hole", "only-too-old", "january-refetches-last-year", "january-empty", "february"],
)
def test_the_years_of_one_run(connection, today: date, stored: tuple[int, ...], expected: list[int]) -> None:
    _store_years(connection, *stored)

    assert years_to_fetch(connection, today=today) == expected


def test_the_instrument_decides_which_rows_count_as_stored_years(connection) -> None:
    seed_risk_free_rate(connection, date(2025, 6, 1), instrument="us_treasury_bill_6_month")

    assert 2025 in years_to_fetch(connection, today=TODAY)


# -- the table itself -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    ["update staging.risk_free_rates_daily set annualized_rate = 0.01", "delete from staging.risk_free_rates_daily"],
    ids=["update", "delete"],
)
def test_the_table_refuses_update_and_delete(connection, statement: str) -> None:
    seed_risk_free_rate(connection, date(2026, 10, 13))

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        connection.execute(statement)


@pytest.mark.parametrize(
    ("knowable_at", "accepted"),
    [
        (datetime(2026, 10, 14, 0, 0, tzinfo=UTC), True),
        (datetime(2026, 10, 13, 0, 0, tzinfo=UTC), False),
        (datetime(2026, 10, 13, 23, 59, 59, tzinfo=UTC), False),
        (datetime(2026, 10, 14, 0, 0, 1, tzinfo=UTC), False),
        (datetime(2026, 10, 15, 0, 0, tzinfo=UTC), False),
    ],
    ids=["next-midnight", "own-midnight", "own-day-end", "one-second-late", "two-days-late"],
)
def test_a_rate_is_never_known_on_its_own_day(connection, knowable_at: datetime, accepted: bool) -> None:
    """Red when the CHECK is dropped: a row that is knowable on its own day would reach a backtest."""
    fetch_id = seed_risk_free_rate(connection, date(2026, 10, 1))
    statement = (
        "insert into staging.risk_free_rates_daily "
        "(instrument, observation_date, annualized_rate, knowable_at, fetch_id, confidence) "
        "values (%s, '2026-10-13', 0.0426, %s, %s, 0.92)"
    )
    if accepted:
        connection.execute(statement, (INSTRUMENT, knowable_at, fetch_id))
    else:
        with pytest.raises(psycopg.errors.CheckViolation, match="knowable_the_next_day"):
            connection.execute(statement, (INSTRUMENT, knowable_at, fetch_id))


@pytest.mark.parametrize(
    ("rate", "confidence"), [("-0.0001", "0.9"), ("1.01", "0.9"), ("0.04", "-0.1"), ("0.04", "1.1")]
)
def test_the_table_bounds_the_rate_and_the_confidence(connection, rate: str, confidence: str) -> None:
    fetch_id = seed_risk_free_rate(connection, date(2026, 10, 1))

    with pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "insert into staging.risk_free_rates_daily "
            "(instrument, observation_date, annualized_rate, knowable_at, fetch_id, confidence) "
            "values (%s, '2026-10-13', %s, '2026-10-14T00:00:00Z', %s, %s)",
            (INSTRUMENT, Decimal(rate), fetch_id, Decimal(confidence)),
        )


def test_a_row_must_point_to_a_stored_fetch(connection) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        connection.execute(
            "insert into staging.risk_free_rates_daily "
            "(instrument, observation_date, annualized_rate, knowable_at, fetch_id, confidence) "
            "values (%s, '2026-10-13', 0.0426, '2026-10-14T00:00:00Z', 2000000000, 0.92)",
            (INSTRUMENT,),
        )


def test_an_empty_table_has_no_rate_for_any_cutoff(connection) -> None:
    with pytest.raises(MissingRiskFreeRate, match="none stored"):
        risk_free_rate_at(connection, datetime(2026, 10, 14, 22, 15, tzinfo=UTC))
