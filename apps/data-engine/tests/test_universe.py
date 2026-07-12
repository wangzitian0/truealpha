"""Listing-selection and code-derivation rules (universe.py) — the exchCode
shapes here mirror what OpenFIGI actually returned on 2026-07-11."""

from data_engine import universe


def _eq(exch, ticker):
    return {"exchCode": exch, "ticker": ticker, "marketSector": "Equity", "securityType": "Common Stock"}


def test_home_market_beats_us_otc_line():
    # Tencent's real shape: the HK line plus a US pink-sheet record on the same ISIN.
    listing = universe.pick_listing([_eq("US", "TCTZF"), _eq("HK", "700")], "KYG875721634")
    assert listing.exch_token == "HK" and listing.ticker == "700"


def test_us_composite_wins_for_us_names():
    listing = universe.pick_listing([_eq("UW", "NVDA"), _eq("US", "NVDA")], "US67066G1040")
    assert listing.exch_token == "US"


def test_us_isin_beats_hkex_secondary_line():
    # Microsoft's real shape: HKEX lists a thin secondary line (HK.04338) under
    # the same US ISIN — the US composite must stay the primary listing.
    listing = universe.pick_listing([_eq("HK", "4338"), _eq("US", "MSFT")], "US5949181045")
    assert listing.exch_token == "US" and listing.ticker == "MSFT"


def test_suffixed_and_unranked_exchcodes_yield_none():
    # MediaTek's real shape: Taiwan (suffixed exchCode) + venue records only.
    assert universe.pick_listing([_eq("TT (Taiwan Stock Exchange)", "2454"), _eq("E1", "2454TWD")]) is None


def test_non_equity_records_ignored():
    rec = dict(_eq("US", "NVDA"), marketSector="Corp")
    assert universe.pick_listing([rec]) is None


def test_moomoo_code_derivation():
    cases = {
        ("US", "NVDA"): ("US.NVDA", 0.98),
        ("US", "BRK/B"): ("US.BRK.B", 0.98),
        ("HK", "700"): ("HK.00700", 0.98),
        ("CG", "600519"): ("SH.600519", 0.98),
        ("CS", "000333"): ("SZ.000333", 0.98),
        ("CH", "300750"): ("SZ.300750", 0.9),  # board inferred from code range
        ("CH", "688111"): ("SH.688111", 0.9),
    }
    for (token, ticker), expected in cases.items():
        listing = universe.Listing(exch_token=token, ticker=ticker, name=None, security_type=None)
        assert universe.moomoo_code(listing) == expected
    unknown_range = universe.Listing(exch_token="CH", ticker="400001", name=None, security_type=None)
    assert universe.moomoo_code(unknown_range) is None


def test_sec_ticker_normalization():
    us = universe.Listing(exch_token="US", ticker="BRK/B", name=None, security_type=None)
    hk = universe.Listing(exch_token="HK", ticker="700", name=None, security_type=None)
    assert universe.sec_ticker(us) == "BRK-B"
    assert universe.sec_ticker(hk) is None


def test_ranked_markets_have_explicit_currency_timezone_and_calendar():
    assert universe.market_metadata("US") == ("USD", "America/New_York", "XNYS")
    assert universe.market_metadata("HK") == ("HKD", "Asia/Hong_Kong", "XHKG")


def test_us_isin_uses_sec_corroborated_fallback_when_openfigi_omits_us_composite():
    # Observed for Exxon (US30231G1022): OpenFIGI returns foreign venues with
    # ticker XOM but no US composite. SEC independently maps XOM to the same
    # normalized issuer name, which is enough to recover the US listing at a
    # lower confidence without a symbol-specific exception.
    records = [
        _eq("PE", "XOM"),
        _eq("CB", "XOM"),
        _eq("CP", "EXMOC"),
    ]
    listing = universe.resolve_listing(
        records,
        isin="US30231G1022",
        issuer_name="Exxon Mobil Corp.",
        sec_ticker_map={"XOM": (34088, "Exxon Mobil Corporation")},
    )
    assert listing is not None
    assert listing.exch_token == "US" and listing.ticker == "XOM"
    assert listing.resolution_method == "openfigi_sec_name_fallback"
    assert universe.moomoo_code(listing) == ("US.XOM", 0.9)


def test_sec_fallback_rejects_name_mismatch_and_non_us_isin():
    records = [_eq("PE", "XOM")]
    ticker_map = {"XOM": (34088, "Exxon Mobil Corporation")}
    assert (
        universe.resolve_listing(
            records,
            isin="US30231G1022",
            issuer_name="Different Company",
            sec_ticker_map=ticker_map,
        )
        is None
    )
    assert (
        universe.resolve_listing(
            records,
            isin="CA0000000001",
            issuer_name="Exxon Mobil Corp.",
            sec_ticker_map=ticker_map,
        )
        is None
    )
