# ADM — company research report

- Report ID: `report:1f5bc6c0c62726e4d77e95fcfc7a6d2322b81fddac8ec84f10343bacfe9dad51`
- Kind: `company`
- Cutoff: `2026-06-30T23:59:59+00:00`
- Source: `fixture:research_report.v1`
- Schema: `research_report.v1`

## issuer:adm

- Subject: `issuer:adm`

### Operating efficiency (capital-adjusted labor efficiency)

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| capital_adjusted_labor_efficiency | 75207.29 USD | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |

### Valuation (three-tier P/S)

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| tier | traditional | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |
| current_price_to_sales | 0.4580 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |
| target_price_to_sales | 1.1500 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |
| valuation_gap | 1.5109 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |

### PEG (switchable conventions)

- Availability: `unavailable`
- Reasons: peg_module_not_materialized

### Supply-chain scenario exposure

- Availability: `unavailable`
- Reasons: supply_chain_module_not_materialized

### Analyst track record

- Availability: `unavailable`
- Reasons: analyst_module_not_materialized

### Large-model-value strategy decision

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| outcome | selected | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |
| eligible | true | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |
| rank | 1 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |
| target_weight | 0.500000 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:24c786a1e5b1:issuer:adm:2026-06-30 |
