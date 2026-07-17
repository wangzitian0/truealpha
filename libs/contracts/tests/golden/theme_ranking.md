# Large-model-value theme ranking

- Report ID: `report:efe11ea3c96e9c1859a9d71cc043205ea58497988a248c89295365cbb0aa06e8`
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
| outcome | selected | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |
| eligible | true | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |
| rank | 1 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |
| target_weight | 0.500000 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |

### Valuation (three-tier P/S)

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| tier | traditional | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |
| current_price_to_sales | 0.4580 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |
| target_price_to_sales | 1.1500 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |
| valuation_gap | 1.5109 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |

## issuer:nice (rank 2)

- Subject: `issuer:nice`

### Large-model-value strategy decision

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| outcome | selected | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:nice:2026-06-30 |
| eligible | true | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:nice:2026-06-30 |
| rank | 2 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:nice:2026-06-30 |
| target_weight | 0.500000 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:nice:2026-06-30 |

### Valuation (three-tier P/S)

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| tier | tech | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:nice:2026-06-30 |
| current_price_to_sales | 1.8639 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:nice:2026-06-30 |
| target_price_to_sales | 4.2500 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:nice:2026-06-30 |
| valuation_gap | 1.2802 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:nice:2026-06-30 |

## issuer:ddog

- Subject: `issuer:ddog`

### Large-model-value strategy decision

- Availability: `low_confidence`
- Factor validation: `not_evaluated`
- Reasons: below_confidence_floor

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| outcome | excluded | 2026-06-30 | low_confidence | 0.65 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:ddog:2026-06-30 |
| eligible | false | 2026-06-30 | low_confidence | 0.65 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:ddog:2026-06-30 |
| rank | — | 2026-06-30 | unavailable | 0.65 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:ddog:2026-06-30 |
| target_weight | — | 2026-06-30 | unavailable | 0.65 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:ddog:2026-06-30 |

### Valuation (three-tier P/S)

- Availability: `low_confidence`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| tier | — | 2026-06-30 | unavailable | 0.65 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:ddog:2026-06-30 |
| current_price_to_sales | — | 2026-06-30 | unavailable | 0.65 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:ddog:2026-06-30 |
| target_price_to_sales | — | 2026-06-30 | unavailable | 0.65 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:ddog:2026-06-30 |
| valuation_gap | — | 2026-06-30 | unavailable | 0.65 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:ddog:2026-06-30 |

## issuer:jpm

- Subject: `issuer:jpm`

### Large-model-value strategy decision

- Availability: `excluded`
- Factor validation: `not_evaluated`
- Reasons: financial_valuation_not_comparable

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| outcome | excluded | 2026-06-30 | excluded | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:jpm:2026-06-30 |
| eligible | false | 2026-06-30 | excluded | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:jpm:2026-06-30 |
| rank | — | 2026-06-30 | unavailable | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:jpm:2026-06-30 |
| target_weight | — | 2026-06-30 | unavailable | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:jpm:2026-06-30 |

### Valuation (three-tier P/S)

- Availability: `excluded`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| tier | — | 2026-06-30 | unavailable | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:jpm:2026-06-30 |
| current_price_to_sales | — | 2026-06-30 | unavailable | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:jpm:2026-06-30 |
| target_price_to_sales | — | 2026-06-30 | unavailable | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:jpm:2026-06-30 |
| valuation_gap | — | 2026-06-30 | unavailable | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:jpm:2026-06-30 |

## issuer:shop

- Subject: `issuer:shop`

### Large-model-value strategy decision

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| outcome | rejected_valuation_above_tier_band | 2026-06-30 | available | 0.80 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:shop:2026-06-30 |
| eligible | true | 2026-06-30 | available | 0.80 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:shop:2026-06-30 |
| rank | — | 2026-06-30 | unavailable | 0.80 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:shop:2026-06-30 |
| target_weight | — | 2026-06-30 | unavailable | 0.80 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:shop:2026-06-30 |

### Valuation (three-tier P/S)

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| tier | large_model_native | 2026-06-30 | available | 0.80 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:shop:2026-06-30 |
| current_price_to_sales | 12.8344 | 2026-06-30 | available | 0.80 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:shop:2026-06-30 |
| target_price_to_sales | 9.0000 | 2026-06-30 | available | 0.80 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:shop:2026-06-30 |
| valuation_gap | -0.2988 | 2026-06-30 | available | 0.80 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:shop:2026-06-30 |
