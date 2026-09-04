"""The headcount reader fuses by declared source priority, never by recency (init.md rule
12; #735 slice 1). A cited extraction outranks the reviewed seed even when the seed's
declared knowable_at is later."""

from datetime import UTC, date, datetime
from decimal import Decimal

from data_engine.datahub.production_topt.headcount import HEADCOUNT_SOURCE_PRIORITY, PostgresHeadcountExtractor


class _Capture:
    def __init__(self, row):
        self.row = row
        self.sql = ""
        self.params = ()

    def execute(self, sql, params):
        self.sql, self.params = " ".join(sql.split()), params
        return self

    def fetchone(self):
        return self.row


def test_the_read_orders_by_source_priority_before_knowable_at() -> None:
    connection = _Capture((Decimal(33000), datetime(2025, 12, 18, tzinfo=UTC)))
    fact = PostgresHeadcountExtractor(connection)(1730168, date(2026, 9, 4))

    assert fact is not None and fact.value == Decimal(33000)
    assert "order by array_position(%s::text[], source) nulls last, knowable_at desc, id desc" in connection.sql
    assert "where cik = %s and knowable_at <= %s" in connection.sql  # still point-in-time
    assert connection.params[2] == list(HEADCOUNT_SOURCE_PRIORITY)
    assert HEADCOUNT_SOURCE_PRIORITY.index("10k-extraction") < HEADCOUNT_SOURCE_PRIORITY.index("manual-review")


def test_no_eligible_row_is_an_honest_none() -> None:
    assert PostgresHeadcountExtractor(_Capture(None))(1, date(2026, 9, 4)) is None
