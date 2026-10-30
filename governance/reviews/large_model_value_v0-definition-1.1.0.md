# Authoring record: large_model_value_v0 definition 1.1.0

A record, not a gate (see `governance/README.md`). It is an authoring record. It is not a review. No owner review of the 1.1.0 file has happened. It states which decision the file implements, what the file holds, and what it does not decide.

## Identity

| Field | Value |
|---|---|
| Strategy | `large_model_value_v0` |
| Definition version | `1.1.0` (`provisional_semantics: false`, the value of 1.0.0) |
| Definition file | `common/contracts/src/truealpha_contracts/data/large_model_value_v0_definition_1_1_0.json` |
| Definition content sha256 | `b8768334a00f88f9f34244a758182454ef220343e161f6c5008dde5c9d4611e3` |
| Strategy definition id | `strategy-definition:b8768334a00f88f9f34244a758182454ef220343e161f6c5008dde5c9d4611e3` |
| Replaces | definition `1.0.0`, sha256 `a0c6f1f003373b5eff85872fd493de91e2fd6d9fa3d15e78e278f96b88bd8803`. The file of 1.0.0 stays: the frozen #21 replay and the stored runs use it. |
| Issue | wangzitian0/truealpha#1518 (step 3), wangzitian0/truealpha#1517 (stage A4), wangzitian0/truealpha#1519 (step 6) |

## Decision that the file implements

Owner decision D2 of 2026-10-26, recorded on #1518: the bank tangible-common-equity rule goes into the next strategy definition. The decision names the bank capital base. It does not name the file. The file is the implementation of the delivery session. Under #1517 stage A4, the confidence floor is removed so decisions carry their continuous confidence score without excluding issuers below 0.70.

The tier cuts, the P/GP bands, the input-age rule, the selection, the sizing and the costs are the values that the owner confirmed for 1.0.0 on 2026-10-12 (see `large_model_value_v0-definition-1.0.0.md`). The file does not change them.

## What the file holds

The definition states no labor-efficiency formula. It pins one tree of the metric forest for each issuer class (`labor_efficiency_tree_keys`), by key, version and content hash:

| Class | Tree key | Tree version | Tree sha256 |
|---|---|---|---|
| `financial` | `gppe_banking_tce` | `v2` | `a6d6ca21a0a92144e46a95054b111ad4e5cd0455dcb7741991e6660e7fc8153b` |
| `non_financial` | `gppe` | `production-topt-v0.2.0` | `9015a3713fd159cf5ba9014eb6ecbb14551da177c14bbd5d7acc17b1e2177906` |
| `insurance` | `gppe` | `production-topt-v0.2.0` | `9015a3713fd159cf5ba9014eb6ecbb14551da177c14bbd5d7acc17b1e2177906` |

`factors.forest.bound_tree` raises when a pin differs from the registered tree. The core and the composite evaluate the same pinned tree.

## Exact difference from 1.0.0

- `definition_version`: `1.0.0` to `1.1.0`.
- `labor_efficiency.factor_version`: `0.1.0` to `0.2.0`.
- `labor_efficiency` states no `numerator`, `capital_charge_base`, `denominator` or `formula`, and no reason code of those inputs (`missing_numerator_reason`, `missing_capital_base_reason`, `missing_denominator_reason`, `nonpositive_denominator_reason`). No code reads any of the four reason codes for any class. They were validated against the formula and hashed.
- `labor_efficiency_tree_keys` is new.
- `eligibility` states no `rule_id`, `minimum_confidence`, `confidence_semantics` or `below_floor_reason` (#1517). Input-age limits (`maximum_input_age_days`, `stale_input_reason`) remain equal to 1.0.0.
- The file holds no `golden_decision_set`. The golden decisions of the frozen #21 replay stay in the file of 1.0.0.

## Working choices of the delivery session

They are not owner approvals. The owner may overrule them.

- A negative tangible common equity gives no labor-efficiency value (`negative_tangible_common_equity` in the composite and in the core). The reason: the capital charge is TCE x rate, and a negative TCE would turn it into a credit. A zero TCE gives a zero charge and a value.
- Preferred stock, goodwill and intangibles are never zero-filled when absent.

## What the file does not decide

- The production release that carries 1.1.0. It waits for the owner, with the exact commit.
- The history on production. The nightly backtest appends rows under the new hash. A manual backtest is a production write.
- The review state. `definition_review_state` returns `reviewed` for every packaged definition with `provisional_semantics: false`. For 1.1.0 that rests on the values of 1.0.0 that the file keeps, and not on a review of the file.
