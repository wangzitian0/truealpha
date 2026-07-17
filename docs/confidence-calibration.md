# Continuous Confidence Calibration v0.1

This is a development sensitivity report for issue #207. It defines a
continuous, explainable score and shows how it responds to source independence,
freshness, semantic quality, lineage, completeness, and conflict. It is not a
Production threshold or a claim that the full TOPT universe is calibrated.

Policy identity:
`confidence-policy:e9493164ba66f91969e54a3b7c82d83e266aa0c55413110b4f2767328e0ef0a5`.

Report identity:
`confidence-calibration-report:968b6f1c8ddd783617c4f4e5655048ff6cc56b9e9623a4ec19c5c0a094d91cd3`.

Regenerate the machine-readable report with:

```bash
uv run --package truealpha-data-engine python apps/data-engine/scripts/report_topt_confidence.py
```

## Formula

For source evidence `i`:

```text
reliability_i = (success_mass_i + 8) / (success_mass_i + failure_mass_i + 10)
q_i = reliability_i * freshness_i * sample_conformance_i * transport_integrity_i
```

With no observed outcome mass, reliability is provisionally capped at `0.8`.
Providers are grouped by canonical original source, so mirrors and wrappers do
not masquerade as independent corroboration:

```text
Q_g = max(q_i for i in origin group g)
E   = sum(independence_weight_g * Q_g)
S   = 1 - exp(-E)
```

The normalized stored score and the human-readable score are:

```text
confidence = S * agreement^0.35
               * semantic_mapping_quality^0.25
               * lineage_completeness^0.20
               * required_component_completeness^0.20

score_100 = 100 * confidence
```

The policy, evidence inputs, origin-group selection, decomposition, and report
are content-addressed. The evaluator uses `Decimal`; binary floats are rejected.
Freshness, availability, applicability, quality state, and reason codes remain
separate queryable facts rather than being replaced by this scalar.

## Sensitivity

| Scenario | Score / 100 | Interpretation |
|---|---:|---|
| One near-perfect independent source | 63.1391 | One origin cannot reach the high-confidence range alone. |
| Two independent agreeing sources | 86.4128 | Independent corroboration raises support materially. |
| Three independent agreeing sources | 94.9916 | Additional support approaches but never equals certainty. |
| Same-origin duplicate | 63.1391 | A mirror is deduplicated and adds no support. |
| Stale source (`freshness=0.5`) | 39.2869 | Cadence-relative staleness lowers evidence continuously. |
| Semantic mismatch (`M=0.5`) | 53.0935 | Mapping ambiguity or definition drift remains visible. |
| Partial lineage (`L=0.5`) | 54.9658 | Broken provenance edges reduce trust. |
| Missing components (`K=0.5`) | 54.9658 | Required cells remain in the denominator and are penalized. |
| Two independent sources in conflict (`A=0.2`) | 49.1970 | Source count cannot hide material disagreement. |
| Yahoo/Twelve Data four-symbol anchor | 74.5972 | Strong OHLC agreement, but evidence is still provisional. |

## Empirical Boundary

The checked-in Yahoo/Twelve Data comparison covers 754 common dates for DDOG,
DUOL, NICE, and SHOP. Across OHLC and volume, 15,069 of 15,080 field checks are
within their declared tolerance. The empirical anchor keeps reliability at the
no-history ceiling and sets required-component completeness to `5/7` because
adjusted close and corporate-action reconciliation are missing.

The frozen TOPT denominator is 20 issuers. Only four have this independent
price-source anchor, so the denominator is not shrunk to four and no Production
calibration claim is made. The remaining 16 issuers, adjusted close, and
corporate actions require additional independent acquisition evidence.

## Review Points

The next revision should settle these policy decisions before any Production
threshold is frozen:

1. Calibrate the `8/2` reliability prior and no-history `0.8` ceiling from held-out outcomes.
2. Define cadence-specific freshness half-lives per semantic requirement.
3. Derive agreement from field-level robust disagreement and versioned tolerances.
4. Register canonical source-origin groups and independently review their weights.
5. Repeat the complete denominator on TOPT, then on QQQ, without dropping abstentions or conflicts.
