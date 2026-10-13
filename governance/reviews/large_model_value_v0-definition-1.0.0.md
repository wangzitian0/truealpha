# Review record: large_model_value_v0 definition 1.0.0

A record, not a gate (see `governance/README.md`). It states who reviewed the definition, what they decided, and the exact content that the decision applies to.

## Identity

| Field | Value |
|---|---|
| Strategy | `large_model_value_v0` |
| Definition version | `1.0.0` (`provisional_semantics: false`) |
| Definition file | `libs/contracts/src/truealpha_contracts/data/large_model_value_v0_definition_1_0_0.json` |
| Definition content sha256 | `a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803` |
| Strategy definition id | `strategy-definition:a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803` |
| Golden decision set id | `golden-decision-set:0712593ce376d1def9f4f355e3fff9f204251bbd784112c8721977949ad1b122` |
| Tier valuation factor id | `factor-definition:35e3aa2af1d3b92d72ce490d217d0490acb1ecd195603e4b4609fcaf23caea9a` |
| P/GP factor id | `factor-definition:e1e4f80941f78be4fb111c6c773ba96de58dc530d3d2f0560d48e9eea5a39530` |
| Replaces | definition `0.1.0`, sha256 `e5c56da455a2845d99e67265d96b30e9b479384ef11a8a4d242494b1816fe79d` (kept loadable for history) |
| Issue | wangzitian0/truealpha#1235 |

New rows carry this sha256 in two places: `mart.strategy_runs.definition_content_sha256` (the strategy tick) and `mart.topt_core_results.tier_definition_sha256` (the TOPT core engine). One file supplies the bands to both engines.

## Reviewer and decision

Reviewer: the owner (wangzitian0), 2026-10-12, in session. The decision is recorded on #1235. The two answers, in order:

1. Valuation basis: "改用毛利口径" (switch to the gross-profit basis). The tier stays capital-adjusted gross profit per employee. The valuation anchor changes from P/S to market value / gross profit (P/GP).
2. Values: "草案+全部档位可选" (the draft, with every tier eligible for selection).

## Confirmed values

| Tier | Labor efficiency (capital-adjusted GP per employee) | Target P/GP | Derivation |
|---|---|---|---|
| traditional | < 1,000,000 | 7.50 – 10.00 | vision P/S 3–4 ÷ reference gross margin 0.40 |
| tech | 1,000,000 – 3,000,000 | 11.43 – 14.29 | vision P/S 8–10 ÷ 0.70 (11.4286, 14.2857, 2 dp half-even) |
| large_model_native | ≥ 3,000,000 | 26.67 – 40.00 | vision P/S 20–30 ÷ 0.75 (26.6667, 2 dp half-even) |

- Boundary rule: lower bound inclusive, upper bound exclusive.
- P/GP = shares outstanding × last unadjusted close ÷ the latest-complete-fiscal-year gross profit that the labor-efficiency numerator uses.
- Gap = target P/GP midpoint ÷ current P/GP − 1 (4 dp half-even). A current P/GP above the band upper bound is `rejected_valuation_above_tier_band`.
- Every tier is eligible. Rank by descending gap, tie-break ascending issuer id, select the top 2, equal weight. These rules and the 0.70 confidence floor are unchanged from 0.1.0.
- Definition 1.0.0 has no gross-margin gate. P/GP removes the margin effect. The hidden 0.35 gate of 0.1.0 was not in the 0.1.0 definition or its hash; it stays for 0.1.0 only, as the named legacy rule `LEGACY_V0_MARGIN_GATE` (`libs/factors/src/factors/composite/three_tier_valuation.py`), so that 0.1.0 history replays exactly.
- P/S is still computed and stored (`current_price_to_sales`). A 1.0.0 decision has no target P/S.

## Replay on the 2026-10-08 production TOPT head

Inputs: capture run `capture-run:338fc733…`, cutoff 2026-10-08. Gross profit and revenue rounded to 10M. The standing check is `apps/data-engine/tests/test_strategy_v1_production_replay.py`. It runs the definition the tick loads through `evaluate_cutoff`. The same test on the code before this change selects JNJ then NFLX and fails.

| Definition | Selected |
|---|---|
| 0.1.0 (recorded on 2026-10-08, with the hidden margin gate) | JNJ (+0.3715), NFLX (+0.3645) |
| 1.0.0 (confirmed) | WMT (+0.7056), META (+0.1661) |

Tier census under 1.0.0: traditional 11, tech 5, large_model_native 1 (NVDA).

| Ticker | Labor efficiency | Tier | P/S | P/GP | Target P/GP | Gap | Outcome | Rank |
|---|---:|---|---:|---:|---:|---:|---|---:|
| NVDA | 3,272,605 | large_model_native | 25.7230 | 36.1959 | 33.3350 | -0.0790 | ranked_beyond_selection_count | 5 |
| META | 1,804,263 | tech | 9.0430 | 11.0284 | 12.8600 | +0.1661 | selected | 2 |
| NFLX | 1,186,574 | tech | 6.5960 | 13.6014 | 12.8600 | -0.0545 | ranked_beyond_selection_count | 4 |
| AAPL | 1,060,468 | tech | 11.9380 | 25.4514 | 12.8600 | -0.4947 | rejected_valuation_above_tier_band | — |
| AVGO | 1,026,867 | tech | 26.9100 | 39.7154 | 12.8600 | -0.6762 | rejected_valuation_above_tier_band | — |
| GOOG | 1,017,723 | tech | 10.4700 | 17.5520 | 12.8600 | -0.2673 | rejected_valuation_above_tier_band | — |
| LLY | 940,257 | traditional | 16.8920 | 20.3403 | 8.7500 | -0.5698 | rejected_valuation_above_tier_band | — |
| MSFT | 841,014 | traditional | 11.6940 | 17.2109 | 8.7500 | -0.4916 | rejected_valuation_above_tier_band | — |
| MA | 751,418 | traditional | 15.8630 | 15.8630 | 8.7500 | -0.4484 | rejected_valuation_above_tier_band | — |
| ABBV | 635,092 | traditional | 7.8710 | 11.2055 | 8.7500 | -0.2191 | rejected_valuation_above_tier_band | — |
| JNJ | 389,898 | traditional | 6.5620 | 9.6665 | 8.7500 | -0.0948 | ranked_beyond_selection_count | 6 |
| AMZN | 193,988 | traditional | 3.8220 | 7.6005 | 8.7500 | +0.1512 | ranked_beyond_selection_count | 3 |
| MU | 154,102 | traditional | 31.2980 | 78.6765 | 8.7500 | -0.8888 | rejected_valuation_above_tier_band | — |
| COST | 100,946 | traditional | 1.3860 | 10.8067 | 8.7500 | -0.1903 | rejected_valuation_above_tier_band | — |
| WMT | 74,439 | traditional | 1.2420 | 5.1302 | 8.7500 | +0.7056 | selected | 1 |
| TSLA | 68,823 | traditional | 15.6190 | 86.6676 | 8.7500 | -0.8990 | rejected_valuation_above_tier_band | — |
| JPM | -514,726 | traditional | 4.8290 | 10.1492 | 8.7500 | -0.1379 | rejected_valuation_above_tier_band | — |

Note on 0.1.0 history: the margin gate lived in code, not in the 0.1.0 definition. The legacy rule keeps it for 0.1.0, so a 0.1.0 replay of the same inputs selects the published JNJ (+0.3715) then NFLX (+0.3645). The same test file holds that check. The rows already in `mart` keep what they recorded.

## State

The code is on the branch for #1235. Production deploy waits for the owner, as for every production change.
