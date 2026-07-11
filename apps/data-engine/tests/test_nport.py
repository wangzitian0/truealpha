"""parse_nport against the committed real filings (samples/nport/) — the QQQ file
is a plain US fund, the ARKK file carries the foreign-holdings pitfalls (CUSIP
placeholder zeros) the parser must normalize."""

from pathlib import Path

from data_engine.sources import nport

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "nport"


def _load(name: str):
    return nport.parse_nport((SAMPLES / name).read_bytes())


def test_qqq_parses_holdings_with_weights():
    info, holdings = _load("QQQ_NPORT_000106783926000024.xml")
    assert info["series_name"] and info["report_period"]
    weighted = [h for h in holdings if h.pct_val is not None]
    assert len(weighted) > 90  # ~100 names
    assert 95.0 < sum(h.pct_val for h in weighted) < 105.0


def test_arkk_placeholder_cusips_normalized_to_none():
    _, holdings = _load("ARKK_NPORT_000094040026025084.xml")
    assert not any(h.cusip == "000000000" for h in holdings)
    # the foreign lines that lose their CUSIP placeholder still resolve via ISIN
    foreign = [h for h in holdings if h.cusip is None]
    assert foreign and any(h.isin for h in foreign)
