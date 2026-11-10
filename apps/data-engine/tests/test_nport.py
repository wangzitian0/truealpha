# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from pathlib import Path; from data_engine.sources import nport; SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "nport"
def _load(name: str): return nport.parse_nport((SAMPLES / name).read_bytes())
def test_qqq_parses_holdings_with_weights(): info, holdings = _load("QQQ_NPORT_000106783926000024.xml"); assert info["series_name"] and info["report_period"]; weighted = [h for h in holdings if h.pct_val is not None]; assert len(weighted) > 90; assert 95.0 < sum(h.pct_val for h in weighted) < 105.0; assert sum(1 for h in weighted if h.value_usd is not None) == len(weighted)
def test_arkk_placeholder_cusips_normalized_to_none(): _, holdings = _load("ARKK_NPORT_000094040026025084.xml"); assert not any(h.cusip == "000000000" for h in holdings); foreign = [h for h in holdings if h.cusip is None]; assert foreign and any(h.isin for h in foreign)
