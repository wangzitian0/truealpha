# Vision — Fundamental & Supply Chain Research Tool

## Why This Exists

This isn't about building a flashy data platform — it's about engineering an already-developed investment framework into something that can be verified repeatably and kept current, without re-pulling data, rebuilding tables, and re-checking conventions by hand every time. The tech stack will keep changing (it already has, multiple times, over the course of this design process); this document won't — it records the part that doesn't move with the tech stack.

## Core Investment Framework (this is what determines which factors matter — none of this is arbitrary)

- **The "large-model-driven company" three-tier framework**: whether a company needs to add headcount proportionally to handle a new category of decisions. Companies are sorted into three tiers, each mapping to a different P/S range. Traditional 3-4x, tech 8-10x, and large-model-native 20-30x are the framework's current illustrative anchors; the executable v1 bands and boundary semantics must be independently reviewed and versioned before use. This is a genuine computed output (init.md module 7), derived from gross profit per employee — not just a mental model for reading that number
- **Gross profit per employee as the core operating metric**: top large-model-native companies can reach roughly $5M in annual gross profit per employee, versus roughly $1M for the prior generation's leaders. This metric captures whether "AI leverage" is actually happening better than revenue growth alone does, and gross profit is defined differently for financial vs. non-financial companies — these need separate handling
- **PEG needs multiple growth-rate conventions**: analyst consensus, historical CAGR, and company guidance can each tell a different story depending on context. The tool needs to support switching between them, not commit to a single convention
- **Supply chain / industry value chain perspective**: upstream-downstream relationships are themselves a source of alpha, not just a side note to financial comparisons
- **Analyst backtesting**: the goal is to verify whose historical judgment has actually been worth trusting, rather than passively accepting consensus
- **ETF-as-virtual-company**: treat a basket of holdings as if it were a single company and look at its fundamentals, to judge whether a given theme or portfolio has real substance behind the packaging
- **"Pure-blood" company screening**: given a theme, find whose revenue exposure is purest — for expressing a specific view precisely, instead of buying a basket of noise

## Use Cases

- **Personal investment research**: when analyzing a new company or theme, historical factors should be reusable and traceable, without rebuilding models by hand each time
- **Content creation**: the AIGC short-form video channel needs material in a "cold verdict" register — the factors, rankings, and reports this tool produces are one source of that raw material, and ultimately feed into Xiaohongshu card decks

## What This Project Is Not

- Not a high-frequency / intraday trading system — the inherent disclosure lag in these data sources (e.g., a ~60-day public lag on ETF holdings) is acceptable
- Not currently a multi-user SaaS product — this is a personal tool; the architecture leaves room to grow but doesn't add complexity for that now
- Not aiming to cover every stock — it serves a curated universe of companies first; depth over breadth

## What Success Looks Like

- Continuously producing results from all seven factor modules for a curated universe (the sensor portfolio and similar + core names of interest), with history that's traceable and conventions that are switchable
- Being able to ask, in conversation, "what's this company's PEG trend" or "who's the purest play on this theme," and get an answer backed by data, traceable to a specific filing/vintage — not a number the model made up on the spot
- Factor/ranking outputs converting directly into Xiaohongshu card material without a manual processing step in between
- Adding a previously unseen company or theme through reviewed, versioned catalog/configuration changes, without changing factor formulas, schemas, or consumer business logic

"Continuously producing" means the versioned, owned curated universe has graduated
beyond canary/shadow operation and meets explicit per-module applicability, usable
coverage, freshness, and traceability objectives across real source refreshes. An
`unavailable`, stale, unresolved, or low-confidence placeholder does not count as a
produced result for an applicable subject. Success also requires the deployed MCP/App/
chat and report/card paths to consume the same materialized outputs; fixture-only tools,
two immediate scheduled runs, or code existence are not completion evidence.

This success state proves a reproducible research product and an honest evaluation path.
It does not by itself prove that any factor, tier, screen, or strategy produces positive
alpha; that requires a separate empirical claim and evidence.
