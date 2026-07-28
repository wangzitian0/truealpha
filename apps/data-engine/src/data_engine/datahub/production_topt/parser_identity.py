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

PARSER_VERSION = "production-topt-live-parser:v3"
MAPPING_VERSION = "production-topt-live-map:v3"

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
