# Large-model-value theme ranking

- Report ID: `report:e9097efdde9afde83b4d11673463f33415a429e082083ad1c707eb8b4baa6f74`
- Kind: `theme_ranking`
- Cutoff: `2026-06-30T23:59:59+00:00`
- Source: `fixture:research_report.v1`
- Schema: `research_report.v1`

## issuer:adm (rank 1)

- Subject: `issuer:adm`

### Large-model-value strategy decision

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| outcome | selected | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:adm:2026-06-30T23:59:59Z |
| eligible | true | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:adm:2026-06-30T23:59:59Z |
| rank | 1 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:adm:2026-06-30T23:59:59Z |
| target_weight | 0.500000 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:adm:2026-06-30T23:59:59Z |

### Valuation (three-tier P/S)

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| tier | traditional | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:adm:2026-06-30T23:59:59Z |
| current_price_to_sales | 0.4580 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:adm:2026-06-30T23:59:59Z |
| target_price_to_sales | 1.1500 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:adm:2026-06-30T23:59:59Z |
| valuation_gap | 1.5109 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:adm:2026-06-30T23:59:59Z |

## issuer:nice (rank 2)

- Subject: `issuer:nice`

### Large-model-value strategy decision

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| outcome | selected | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:nice:2026-06-30T23:59:59Z |
| eligible | true | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:nice:2026-06-30T23:59:59Z |
| rank | 2 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:nice:2026-06-30T23:59:59Z |
| target_weight | 0.500000 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:nice:2026-06-30T23:59:59Z |

### Valuation (three-tier P/S)

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| tier | tech | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:nice:2026-06-30T23:59:59Z |
| current_price_to_sales | 1.8639 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:nice:2026-06-30T23:59:59Z |
| target_price_to_sales | 4.2500 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:nice:2026-06-30T23:59:59Z |
| valuation_gap | 1.2802 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:nice:2026-06-30T23:59:59Z |

## issuer:ddog

- Subject: `issuer:ddog`

### Large-model-value strategy decision

- Availability: `low_confidence`
- Factor validation: `not_evaluated`
- Reasons: below_confidence_floor

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| outcome | excluded | 2026-06-30 | low_confidence | 0.65 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:ddog:2026-06-30T23:59:59Z |
| eligible | false | 2026-06-30 | low_confidence | 0.65 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:ddog:2026-06-30T23:59:59Z |
| rank | — | 2026-06-30 | unavailable | 0.65 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:ddog:2026-06-30T23:59:59Z |
| target_weight | — | 2026-06-30 | unavailable | 0.65 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:ddog:2026-06-30T23:59:59Z |

### Valuation (three-tier P/S)

- Availability: `low_confidence`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| tier | — | 2026-06-30 | unavailable | 0.65 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:ddog:2026-06-30T23:59:59Z |
| current_price_to_sales | — | 2026-06-30 | unavailable | 0.65 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:ddog:2026-06-30T23:59:59Z |
| target_price_to_sales | — | 2026-06-30 | unavailable | 0.65 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:ddog:2026-06-30T23:59:59Z |
| valuation_gap | — | 2026-06-30 | unavailable | 0.65 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:ddog:2026-06-30T23:59:59Z |

## issuer:jpm

- Subject: `issuer:jpm`

### Large-model-value strategy decision

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| outcome | rejected_valuation_above_tier_band | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:jpm:2026-06-30T23:59:59Z |
| eligible | true | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:jpm:2026-06-30T23:59:59Z |
| rank | — | 2026-06-30 | unavailable | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:jpm:2026-06-30T23:59:59Z |
| target_weight | — | 2026-06-30 | unavailable | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:jpm:2026-06-30T23:59:59Z |

### Valuation (three-tier P/S)

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| tier | traditional | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:jpm:2026-06-30T23:59:59Z |
| current_price_to_sales | 4.8388 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:jpm:2026-06-30T23:59:59Z |
| target_price_to_sales | 1.1500 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:jpm:2026-06-30T23:59:59Z |
| valuation_gap | -0.7623 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:jpm:2026-06-30T23:59:59Z |

## issuer:shop

- Subject: `issuer:shop`

### Large-model-value strategy decision

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| outcome | rejected_valuation_above_tier_band | 2026-06-30 | available | 0.80 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:shop:2026-06-30T23:59:59Z |
| eligible | true | 2026-06-30 | available | 0.80 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:shop:2026-06-30T23:59:59Z |
| rank | — | 2026-06-30 | unavailable | 0.80 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:shop:2026-06-30T23:59:59Z |
| target_weight | — | 2026-06-30 | unavailable | 0.80 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:shop:2026-06-30T23:59:59Z |

### Valuation (three-tier P/S)

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| tier | large_model_native | 2026-06-30 | available | 0.80 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:shop:2026-06-30T23:59:59Z |
| current_price_to_sales | 12.8344 | 2026-06-30 | available | 0.80 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:shop:2026-06-30T23:59:59Z |
| target_price_to_sales | 9.0000 | 2026-06-30 | available | 0.80 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:shop:2026-06-30T23:59:59Z |
| valuation_gap | -0.2988 | 2026-06-30 | available | 0.80 | large_model_value_v0 | strategy_smoke_fixture:8cdb081d887f:issuer:shop:2026-06-30T23:59:59Z |
