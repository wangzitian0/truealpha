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
    "production-topt-live-parser:v6",
    "production-topt-live-parser:v7",
    "production-topt-live-parser:v8",
)
MAPPING_VERSION_HISTORY = (
    "production-topt-live-map:v1",
    "production-topt-live-map:v2",
    "production-topt-live-map:v3",
    "production-topt-live-map:v4",
    "production-topt-live-map:v5",
    "production-topt-live-map:v6",
    "production-topt-live-map:v7",
    "production-topt-live-map:v8",
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
#
# v5 -> v6 (#284, owner decision 2026-08-17): `earnings_cagr_3y` stops being an endpoint
# CAGR and becomes the recency-weighted mean of the year-over-year rates inside the window,
# weighted 1..n oldest to newest. It also now requires an observation at every year
# boundary rather than only the two ends. The same issuer and window therefore resolves a
# DIFFERENT number under v6 than under v5 -- exactly the ambiguity a version exists to
# prevent -- and some issuers resolve none where v5 resolved one.
#
# v6 -> v7 (#284) carries two changes, and the second is the one that matters in a
# warehouse:
#
# 1. `net_income` gained `ProfitLoss` as a second variant. AVGO's FY2025 figure exists in
#    company-facts only under that tag, so v6 resolved no recent endpoint and left PEG
#    unavailable for an issuer whose earnings were on file. The variant resolves a value
#    where v6 resolved none, and for an issuer with material noncontrolling interests it
#    would resolve a DIFFERENT value, which is why it takes a version rather than an edit.
#
# 2. v5 and v6 never resolved the growth basis IN THE DEPLOYED PATH AT ALL. `build_bundle`
#    gated `net_income` and `earnings_cagr_3y` behind `earnings_cagr_years`, which defaulted
#    to None meaning "skip", and `sec_financial_fetcher` — the only deployed caller — never
#    passed it. Every v6 observation in both environments carries both keys with a null
#    value (21/21 in Staging, checked in SQL), and `mart.strategy_decisions.peg` is NULL for
#    all 600 Production and 4,260 Staging decisions. The runs reported SUCCESS throughout.
#    v7 is therefore the first vintage under which these two fields mean "the source
#    asserted nothing" rather than "nobody asked" — a v6 null and a v7 null are different
#    claims about the same issuer, which is the whole reason this tuple exists.
#
# Both land in one vintage because v7 was never deployed: no observation anywhere carries
# it, so there is no v6/v7 boundary in the warehouse to keep clean between them, and (1) is
# unobservable in production without (2).

# v7 -> v8 (#284): the payload stops carrying `earnings_cagr_3y` and its two endpoints and
# carries `net_income_by_period` instead -- the annual series itself. The rate is no longer
# computed here at all; `factors.base.peg` reduces the series, which is where init.md rule 2
# says factor arithmetic lives. It ran in this adapter only because the input transport had
# no period axis and a series could not cross; migration 0043 gives it one.
#
# The published PEG values do NOT move: the moved arithmetic is bit-identical, verified
# against the adapter implementation over the real JNJ, AVGO, steady, loss-year and
# missing-year shapes before the old one was deleted. It still takes a version because a
# v7 payload and a v8 payload answer different questions -- v7 asserts a rate, v8 asserts
# the observations a rate is derived from -- and `knowable_at` now spans EVERY period in
# the series rather than the window's two endpoints, which is a different claim about when
# the payload became knowable (and the PIT obligation #284 named).
