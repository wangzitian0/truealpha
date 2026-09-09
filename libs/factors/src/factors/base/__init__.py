"""Base factors — modules 1-6 (init.md §7).

They consume provenance-neutral inputs the runner projects for them: usually staging
(incl. KG) facts, and for `etf_virtual_company` (module 5) a fund's filed weights beside
another module's already-materialized output. What makes a factor base rather than
composite is that it does not load mart ITSELF — only module 7 does that
(`factors.composite`), and only it may be registered `kind="composite"`.
"""
