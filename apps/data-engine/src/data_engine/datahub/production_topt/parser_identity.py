"""The parser identity every primary TOPT observation is recorded under.

One capture run's four semantics are parsed by three different adapters, but they share a
single parser/mapping version because downstream selection keys on it: the quality report
resolves a market-price cell's origin group from `parser_version`, and the capture→strategy
bridge selects which parser vintage of a run's observations crosses into
`staging.strategy_backtest_inputs`. Those two would silently diverge if each adapter minted
its own string, so the shared identity lives here rather than as three copies that have to
agree by luck.

A reparse changes these versions and appends new observations; it never rewrites history
(init.md: parsed facts carry `mapping_version` so reparses stay distinguishable from
restatements).
"""

from __future__ import annotations

# Every primary parser vintage ever shipped, oldest first. The current version is the LAST
# entry rather than a separate constant, so a bump and the record of it are one edit: there
# is no way to advance `PARSER_VERSION` while forgetting that the previous vintage still
# exists in the warehouse.
#
# That mattered (#543). Downstream code keys on the vintage — `quality_report`'s
# `_SOURCE_BY_PARSER` resolves a market-price cell's origin group from it — and such a map
# listed only the imported current version plus a v1 literal. Importing the current version
# protected the current version and nothing else, so when v4 shipped, every observation in
# both warehouses (all v3) fell out of the map and a report over any historical run resolved
# `insufficient_independent_origins` for all 21 cells, silently, for runs that had in fact
# agreed 21/21 across two origins. History is the half that a "current version is mapped"
# guard cannot cover; a map built from this tuple covers it by construction.
PARSER_VERSION_HISTORY = (
    "production-topt-live-parser:v1",
    "production-topt-live-parser:v2",
    "production-topt-live-parser:v3",
    "production-topt-live-parser:v4",
    "production-topt-live-parser:v5",
)
MAPPING_VERSION_HISTORY = (
    "production-topt-live-map:v1",
    "production-topt-live-map:v2",
    "production-topt-live-map:v3",
    "production-topt-live-map:v4",
    "production-topt-live-map:v5",
)

PARSER_VERSION = PARSER_VERSION_HISTORY[-1]
MAPPING_VERSION = MAPPING_VERSION_HISTORY[-1]

# v1 -> v2 (#496): concept selection changed from "first variant carrying any value" to
# "latest period across synonym variants", restatements began resolving by filing date
# rather than JSON array position, share counts gained the `dei` cover-page concept, and
# the payload gained `operating_period_end` / `revenue_period_end`.
#
# Both strings move together because they identify one parse. Leaving them at v1 would
# make every corrected figure indistinguishable from a restatement of the old one: the
# same issuer, the same period, a different number, under the same parser identity — which
# is precisely the ambiguity `mapping_version` exists to prevent. The v1 observations stay
# in place and stay queryable; v2 appends alongside them.
#
# v3 (#514): the numerator gained branch/tag policies — a revenue proxy for issuers that
# report no COGS family, the insurance revenue-minus-claims branch, and weighted-average
# shares as a versioned fallback for issuers whose per-class cover-page facts the
# company-facts API omits.
#
# v3 -> v4 (#529): a share count measured more than `_MAX_SHARES_STALENESS_DAYS` before the
# cutoff is refused instead of used, and the payload gained `shares_period_end` so the
# refusal is auditable in SQL rather than only reproducible by re-deriving from the vendor.
# This changes a resolved value (V's shares go from a 2010 figure to absent), which is why
# it takes a version rather than a silent correction: under the old identity the two would
# be the same issuer, the same period and a different number.
#
# v4 -> v5 (#284): the ruleset gained `net_income` and the payload gained
# `earnings_cagr_3y` with both of its period endpoints, so module 1 can compute PEG from
# the annual series company-facts already carries. The parse resolves a field it did not
# before, which is a new parse rather than a restatement of the old one -- under the v4
# identity an observation with and without the growth basis would be indistinguishable.
