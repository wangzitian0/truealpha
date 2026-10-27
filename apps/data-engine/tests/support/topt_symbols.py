"""The symbols a test seeds as the TOPT price series (#1522).

The market-data lane reads them from the TOPT corpus and the primary-listing relation
(`market_prices.stored_symbols`). A test that seeds series before a tick cannot read the relation, because the
tick writes it. This helper reads the same two sources as files: the packaged corpus and the classification
file of primary listings that the coordinate resolver writes the relation from. No test holds this helper
equal to `stored_symbols` against a database (a known residual of #1522 step 3); the tick tests that use it
(`test_primary_listing_one_producer.py` runs the real op over a seeded relation) fail when the two differ.
"""

from __future__ import annotations

from data_engine.datahub.production_topt.universe_corpus import TOPT_CORPUS_FILENAME, load_corpus
from data_engine.datahub.resolve_coordinates import primary_listing_ids


def topt_symbols() -> tuple[str, ...]:
    """The ticker of the primary listing of each TOPT issuer, by symbol."""
    denominator = load_corpus(TOPT_CORPUS_FILENAME)["topt_denominator"]
    fields = list(denominator["instrument_tuple_fields"])
    issuer_at, listing_at, ticker_at = (fields.index(name) for name in ("issuer_id", "listing_id", "ticker"))
    declared = primary_listing_ids()
    by_issuer: dict[str, list[tuple[str, str]]] = {}
    for row in denominator["instruments"]:
        by_issuer.setdefault(str(row[issuer_at]), []).append((str(row[listing_at]), str(row[ticker_at])))
    chosen = [
        rows[0][1] if len(rows) == 1 else next((ticker for listing, ticker in rows if listing in declared), None)
        for rows in by_issuer.values()
    ]
    return tuple(sorted(ticker for ticker in chosen if ticker))


TOPT_SYMBOLS = topt_symbols()
