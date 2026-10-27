# Authoring record: large_model_value_v0 definition 1.1.0

A record, not a gate (see `governance/README.md`). It is an authoring record. It is not a review. No owner review of the 1.1.0 file has happened. It states which decision the file implements, what the file holds, and what it does not decide.

## Identity

| Field | Value |
|---|---|
| Strategy | `large_model_value_v0` |
| Definition version | `1.1.0` (`provisional_semantics: false`, the value of 1.0.0) |
| Definition file | `common/contracts/src/truealpha_contracts/data/large_model_value_v0_definition_1_1_0.json` |
| Definition content sha256 | `8d1462dfaa27516ab29cdc262dbe54eabe6e6a068d95985ef4462e3d8a78fbb0` |
| Strategy definition id | `strategy-definition:8d1462dfaa27516ab29cdc262dbe54eabe6e6a068d95985ef4462e3d8a78fbb0` |
| Replaces | definition `1.0.0`, sha256 `a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803`. The file of 1.0.0 stays: the frozen #21 replay and the stored runs use it. |
| Issue | wangzitian0/truealpha#1518 (step 3) |

## Decision that the file implements

Owner decision D2 of 2026-10-26, recorded on #1518: the bank tangible-common-equity rule goes into the next strategy definition. The decision names the bank capital base. It does not name the file. The file is the implementation of the delivery session.

The tier cuts, the P/GP bands, the eligibility rule, the selection, the sizing and the costs are the values that the owner confirmed for 1.0.0 on 2026-10-12 (see `large_model_value_v0-definition-1.0.0.md`). The file does not change them.

## What the file holds

The definition states no labor-efficiency formula. It pins one tree of the metric forest for each issuer class (`labor_efficiency_tree_keys`), by key, version and content hash:

| Class | Tree key | Tree version | Tree sha256 |
|---|---|---|---|
| `financial` | `gppe_banking_tce` | `v2` | `65a5ecb0e9baf1df506748505c90f4d66411c698e94224a26c8b3247d25a32cd` |
| `non_financial` | `gppe` | `production-topt-v0.2.0` | `2fdad23895c026e4e6b14cb808f794e1d8936666097506122ba8dc3c55e0c26d` |
| `insurance` | `gppe` | `production-topt-v0.2.0` | `2fdad23895c026e4e6b14cb808f794e1d8936666097506122ba8dc3c55e0c26d` |

`factors.forest.bound_tree` raises when a pin differs from the registered tree. The core and the composite evaluate the same pinned tree.

## Exact difference from 1.0.0

- `definition_version`: `1.0.0` to `1.1.0`.
- `labor_efficiency.factor_version`: `0.1.0` to `0.2.0`.
- `labor_efficiency` states no `numerator`, `capital_charge_base`, `denominator` or `formula`, and no reason code of those inputs (`missing_numerator_reason`, `missing_capital_base_reason`, `missing_denominator_reason`, `nonpositive_denominator_reason`). No code reads any of the four reason codes for any class. They were validated against the formula and hashed.
- `labor_efficiency_tree_keys` is new.
- The file holds no `golden_decision_set`. The golden decisions of the frozen #21 replay stay in the file of 1.0.0.

## Working choices of the delivery session

They are not owner approvals. The owner may overrule them.

- A negative tangible common equity gives no labor-efficiency value (`negative_tangible_common_equity` in the composite and in the core). The reason: the capital charge is TCE x rate, and a negative TCE would turn it into a credit. A zero TCE gives a zero charge and a value.
- Preferred stock, goodwill and intangibles are never zero-filled when absent.

## What the file does not decide

- The production release that carries 1.1.0. It waits for the owner, with the exact commit.
- The history on production. The nightly backtest appends rows under the new hash. A manual backtest is a production write.
- The review state. `definition_review_state` returns `reviewed` for every packaged definition with `provisional_semantics: false`. For 1.1.0 that rests on the values of 1.0.0 that the file keeps, and not on a review of the file.
