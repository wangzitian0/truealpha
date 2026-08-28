"""The N-PORT holdings plane, first tranche (#63): the fund's own filed weights,
captured weekly from the deployed root.

What this covers of #63: immutable capture (content-addressed raw landing),
fund/series identity in the KG, report-period vs filing-date (knowable) time
split, per-line ISIN/CUSIP/LEI + balance/value/weight with raw lineage, and
append-only vintages via uq_fund_holding_line_vintage. What it deliberately does
NOT cover yet: instrument-level listing resolution (GOOG vs GOOGL as held
instruments), typed policies for derivative/short/debt lines beyond the equity
filter, partitioned assets, and the #61 resolved-weight SLO.

Holding identity is KG-first: an ISIN already known to staging.kg_identifiers
resolves to its existing entity (bootstrap vintages included); an unseen ISIN
mints a durable company:isin:<isin> entity that later enrichment re-points by
asserting a newer identifier vintage — the same pointer rule the rest of the KG
follows. Lines with no ISIN at all are counted and skipped, not guessed
(bootstrap's log-don't-guess rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from factors.shared import entity_resolution as er
from truealpha_contracts.models import DataSource

from data_engine import raw_store
from data_engine.sources import nport, sec

# The filing itself is the assertion; same constant bootstrap_universe uses.
CONF_FILING = 1.0


def _exact(value: float | None) -> Decimal | None:
    """Filed numbers land in NUMERIC columns; going through str strips the
    binary-float representation error a raw float adapter would keep (#682)."""
    return None if value is None else Decimal(str(value))


@dataclass(frozen=True)
class HoldingsOutcome:
    """One fund's capture, sized for the op log."""

    etf: str
    accession: str
    report_period: str
    filing_date: str
    raw_id: int
    equity_lines: int
    facts_inserted: int
    holdings_resolved: int  # ISIN already known to the KG
    holdings_minted: int  # unseen ISIN -> company:isin:<isin>
    lines_skipped: int  # no ISIN or no valUSD — logged, not guessed

    def __str__(self) -> str:
        return (
            f"nport[{self.etf}]: {self.accession} period={self.report_period} "
            f"filed={self.filing_date} raw={self.raw_id} lines={self.equity_lines} "
            f"inserted={self.facts_inserted} kg={self.holdings_resolved} "
            f"minted={self.holdings_minted} skipped={self.lines_skipped}"
        )


def capture_fund_holdings(connection, etf_ticker: str, *, http=None) -> HoldingsOutcome:
    """Fetch the fund's latest N-PORT, land the bytes, write line facts.

    Idempotent per filing: the raw landing is content-addressed, identifier and
    edge asserts skip when the latest vintage already says the same, and the
    fact INSERT lands on uq_fund_holding_line_vintage. A re-run of an unchanged
    filing inserts nothing.
    """
    client = http or sec.client()
    cik, series_id = nport.fund_series(client, etf_ticker)
    accession, filing_date = nport.latest_nport_accession(client, series_id)
    xml = nport.fetch_nport_xml(client, cik, accession)
    info, all_holdings = nport.parse_nport(xml)
    filing_dt = datetime.fromisoformat(filing_date).replace(tzinfo=UTC)

    raw_id = raw_store.insert_fetch(
        connection,
        source=DataSource.NPORT,
        source_record_id=f"{etf_ticker}:{accession}",
        body=xml,
        content_type="application/xml",
        fetched_at=datetime.now(UTC),
        source_published_at=filing_dt,
        metadata={"cik": cik, "series_id": series_id, "ticker": etf_ticker},
    )
    filing_ref = raw_store.raw_ref(raw_id)

    # Equity lines only: N-PORT also lists cash sweeps, futures, repos. Lines
    # without an assetCat are kept (the field is optional); non-equities among
    # those are the later listing-resolution tranche's problem, not a guess here.
    holdings = [h for h in all_holdings if h.asset_cat in (None, "EC") and h.pct_val is not None]

    fund_id = f"etf:series:{series_id}"
    # One fallback, used everywhere report_period appears — outcome, valid_from
    # and the fact rows must agree on the vintage they describe (review on #682).
    report_period = info["report_period"] or filing_date
    er.ensure_entity(connection, fund_id, "etf", info["series_name"] or etf_ticker.upper())
    for id_type, id_value in (("ticker", etf_ticker.upper()), ("sec_series", series_id)):
        er.assert_identifier(
            connection,
            entity_id=fund_id,
            source="sec",
            identifier_type=id_type,
            identifier_value=id_value,
            confidence=CONF_FILING,
            transaction_time=filing_dt,
            valid_from=report_period,
            raw_ref=filing_ref,
        )

    inserted = resolved = minted = skipped = 0
    edged: set[str] = set()
    for h in holdings:
        if h.isin is None or h.value_usd is None:
            skipped += 1
            continue
        holding_id = er.resolve(connection, "isin", h.isin, as_of=filing_dt)
        if holding_id is None:
            holding_id = f"company:isin:{h.isin}"
            er.ensure_entity(connection, holding_id, "company", h.name or h.isin)
            er.assert_identifier(
                connection,
                entity_id=holding_id,
                source="nport",
                identifier_type="isin",
                identifier_value=h.isin,
                confidence=CONF_FILING,
                transaction_time=filing_dt,
                valid_from=report_period,
                raw_ref=filing_ref,
            )
            minted += 1
        else:
            resolved += 1
        if holding_id not in edged:
            edged.add(holding_id)
            er.add_edge(
                connection,
                from_id=fund_id,
                to_id=holding_id,
                relation_type="holds",
                confidence=CONF_FILING,
                source="nport",
                transaction_time=filing_dt,
                valid_from=report_period,
                raw_ref=filing_ref,
            )
        result = connection.execute(
            """
            insert into staging.fund_holding_facts
                (fund_id, holding_id, holding_name, report_period, transaction_time,
                 cusip, isin, lei, balance, value_usd, percent_of_net_assets, confidence, raw_ref)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict do nothing
            """,
            (
                fund_id,
                holding_id,
                h.name or h.isin,
                report_period,
                filing_dt,
                h.cusip,
                h.isin,
                h.lei,
                _exact(h.balance),
                _exact(h.value_usd),
                _exact(h.pct_val),
                CONF_FILING,
                filing_ref,
            ),
        )
        inserted += result.rowcount

    return HoldingsOutcome(
        etf=etf_ticker.upper(),
        accession=accession,
        report_period=report_period,
        filing_date=filing_date,
        raw_id=raw_id,
        equity_lines=len(holdings),
        facts_inserted=inserted,
        holdings_resolved=resolved,
        holdings_minted=minted,
        lines_skipped=skipped,
    )
