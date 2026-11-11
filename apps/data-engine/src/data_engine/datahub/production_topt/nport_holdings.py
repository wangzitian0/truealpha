"""N-PORT fund holdings capture and append-only line fact persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from factors.shared import entity_resolution as er
from truealpha_contracts.models import DataSource

from data_engine import raw_store
from data_engine.sources import nport, sec

CONF_FILING = 1.0


def _exact(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


@dataclass(frozen=True)
class HoldingsOutcome:
    etf: str
    accession: str
    report_period: str
    filing_date: str
    raw_id: int
    equity_lines: int
    facts_inserted: int
    holdings_resolved: int
    holdings_minted: int
    lines_skipped: int

    def __str__(self) -> str:
        return (
            f"nport[{self.etf}]: {self.accession} period={self.report_period} "
            f"filed={self.filing_date} raw={self.raw_id} lines={self.equity_lines} "
            f"inserted={self.facts_inserted} kg={self.holdings_resolved} "
            f"minted={self.holdings_minted} skipped={self.lines_skipped}"
        )


def capture_fund_holdings(connection, etf_ticker: str, *, http=None) -> HoldingsOutcome:
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
    holdings = [h for h in all_holdings if h.asset_cat in (None, "EC") and h.pct_val is not None]

    fund_id = f"etf:series:{series_id}"
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
