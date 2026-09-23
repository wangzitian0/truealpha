# ADM — company research report

- Report ID: `report:1d4f66256d975c2f0cb80b4de6027b500db76847cf0234499ce8ad1dc4979a5c`
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
| capital_adjusted_labor_efficiency | 75207.29 USD | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:16f5e0b8839e:issuer:adm:2026-06-30T23:59:59Z |

### Valuation (three-tier P/S)

- Availability: `available`
- Factor validation: `not_evaluated`

| Result | Value | Period | Availability | Confidence | Factor version | Trace |
| --- | --- | --- | --- | --- | --- | --- |
| tier | traditional | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:16f5e0b8839e:issuer:adm:2026-06-30T23:59:59Z |
| current_price_to_sales | 0.4580 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:16f5e0b8839e:issuer:adm:2026-06-30T23:59:59Z |
| target_price_to_sales | 1.1500 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:16f5e0b8839e:issuer:adm:2026-06-30T23:59:59Z |
| valuation_gap | 1.5109 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:16f5e0b8839e:issuer:adm:2026-06-30T23:59:59Z |

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
| outcome | selected | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:16f5e0b8839e:issuer:adm:2026-06-30T23:59:59Z |
| eligible | true | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:16f5e0b8839e:issuer:adm:2026-06-30T23:59:59Z |
| rank | 1 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:16f5e0b8839e:issuer:adm:2026-06-30T23:59:59Z |
| target_weight | 0.500000 | 2026-06-30 | available | 0.90 | large_model_value_v0 | strategy_smoke_fixture:16f5e0b8839e:issuer:adm:2026-06-30T23:59:59Z |
