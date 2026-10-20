# Vision — Fundamental & Supply Chain Research Tool

## Why This Exists

This isn't about building a flashy data platform — it's about engineering an already-developed investment framework into something that can be verified repeatably and kept current, without re-pulling data, rebuilding tables, and re-checking conventions by hand every time. The tech stack will keep changing (it already has, multiple times, over the course of this design process); this document won't — it records the part that doesn't move with the tech stack.

## Core Investment Framework (this is what determines which factors matter — none of this is arbitrary)

- **The "large-model-driven company" three-tier framework**: whether a company needs to add headcount proportionally to handle a new category of decisions. Companies are sorted into three tiers, each mapping to a different P/S range. Traditional 3-4x, tech 8-10x, and large-model-native 20-30x are the framework's current illustrative anchors; the executable v1 bands and boundary semantics must be independently reviewed and versioned before use. The owner reviewed them on 2026-10-12 (#1235): strategy definition 1.0.0 values by P/GP = market value / gross profit, with bands equal to these P/S anchors divided by a reference gross margin. The values live in one definition file; the review record is `governance/reviews/large_model_value_v0-definition-1.0.0.md`. This is a genuine computed output (init.md module 7), derived from gross profit per employee — not just a mental model for reading that number
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
- Not aiming to cover every stock. The production target is the S&P 500 (owner decision 2026-10-20, #1410; governed by `common/contracts/src/truealpha_contracts/data/environment_contract.v1.json`, #1416). "Depth over breadth" means that each company in that universe gets all seven modules with traceable history before any name outside it is added

## What Success Looks Like

- Continuously producing results from all seven factor modules for the production universe, the S&P 500, with history that's traceable and conventions that are switchable
- Being able to ask, in conversation, "what's this company's PEG trend" or "who's the purest play on this theme," and get an answer backed by data, traceable to a specific filing/vintage — not a number the model made up on the spot
- Factor/ranking outputs converting directly into Xiaohongshu card material without a manual processing step in between
- Adding a previously unseen company or theme through reviewed, versioned catalog/configuration changes, without changing factor formulas, schemas, or consumer business logic
- Adding a source for an existing semantic type through one isolated adapter/normalizer registration, and adding a semantic record type through one typed schema/storage/projector registration, without source/type branches in the generic capture, snapshot, factor-runner, lineage, usage, or consumer path
- *Demoted (owner decision 2026-10-20, #1410; init.md rules 23 and 24):* querying how often declared data is captured and actually consumed, and running an immutable strategy-to-source data-quality review that exposes required-but-missing inputs and the decisions they affected. Restore this item when the core path is green in production for two nights in a row, the exit condition of `governance/freezes/core-path-2026-10-12.md`

"Continuously producing" means the versioned production universe has graduated
beyond canary/shadow operation and meets explicit per-module applicability, usable
coverage, freshness, and traceability objectives across real source refreshes. An
`unavailable`, stale, unresolved, or sentinel placeholder does not count as a
produced result for an applicable subject. A computed value with a low confidence score
counts, and its score is shown beside it (owner decision 2026-10-15, #1333). Success also requires the deployed MCP/App/
chat and report/card paths to consume the same materialized outputs; fixture-only tools,
two immediate scheduled runs, or code existence are not completion evidence.

This success state proves a reproducible research product and an honest evaluation path.
It does not by itself prove that any factor, tier, screen, or strategy produces positive
alpha; that requires a separate empirical claim and evidence.

## Capabilities and where they live

Delivery state lives on the issues and on the dashboards (`/admin/datahub`), not in this file.

| Capability / Factor Module | Architecture Reference | Target Scope |
|---|---|---|
| **Module 1: PEG (Switchable Conventions)** | `factors.base.peg` | Historical CAGR, Consensus, Guidance |
| **Module 2: Gross Profit per Employee** | `factors.base.gross_profit_per_employee` | Operating AI leverage |
| **Module 3: Supply Chain / Value Chain** | `factors.base.supply_chain_exposure` | Upstream-downstream alpha |
| **Module 4: Analyst Backtesting** | `factors.base.analyst_track_record` | Historical analyst accuracy |
| **Module 5: ETF-as-Virtual-Company** | `factors.base.etf_virtual_company` | Holdings fundamentals aggregation |
| **Module 6: Pure-Blood Company Screening** | `factors.base.theme_purity` | Segment revenue exposure purity |
| **Module 7: Three-Tier Valuation Gap** | `factors.composite.three_tier_valuation` | P/GP tier gap against GPPE (definition 1.0.0, #1235) |
| **Xiaohongshu Card Deck Generation** | `truealpha_contracts.research_cards` | Automated material export |
| **Conversational Research Q&A** | `apps/llm-service` MCP | Natural language fact retrieval |
