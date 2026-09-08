-- #747 (init.md §8): the three status dimensions on every factor row, written by the
-- producer (materialization / strategy writer) from what it consumed — never by a
-- consumer. Vocabularies are the contracts' enums (`truealpha_contracts.execution`):
--   availability_status        available | unavailable | stale | excluded | low_confidence | error
--   source_evidence_status     verified | degraded | rejected      (InputEvidenceStatus)
--   factor_validation_status   accepted | rejected | not_evaluated (FactorValidationStatus)
-- Nullable: rows written before this migration carry no dimension rather than a guess.
alter table mart.topt_core_results
    add column if not exists availability_status text
        check (availability_status in ('available', 'unavailable', 'stale', 'excluded', 'low_confidence', 'error'));
alter table mart.topt_core_results
    add column if not exists source_evidence_status text
        check (source_evidence_status in ('verified', 'degraded', 'rejected'));
alter table mart.topt_core_results
    add column if not exists factor_validation_status text
        check (factor_validation_status in ('accepted', 'rejected', 'not_evaluated'));
alter table mart.topt_gppe_results
    add column if not exists availability_status text
        check (availability_status in ('available', 'unavailable', 'stale', 'excluded', 'low_confidence', 'error'));
alter table mart.topt_gppe_results
    add column if not exists source_evidence_status text
        check (source_evidence_status in ('verified', 'degraded', 'rejected'));
alter table mart.topt_gppe_results
    add column if not exists factor_validation_status text
        check (factor_validation_status in ('accepted', 'rejected', 'not_evaluated'));
alter table mart.strategy_decisions
    add column if not exists availability_status text
        check (availability_status in ('available', 'unavailable', 'stale', 'excluded', 'low_confidence', 'error'));
alter table mart.strategy_decisions
    add column if not exists source_evidence_status text
        check (source_evidence_status in ('verified', 'degraded', 'rejected'));
alter table mart.strategy_decisions
    add column if not exists factor_validation_status text
        check (factor_validation_status in ('accepted', 'rejected', 'not_evaluated'));
comment on column mart.topt_core_results.availability_status is '#747 / init.md §8: availability of this factor row (contracts AvailabilityStatus); reason_codes carry why.';
comment on column mart.topt_core_results.source_evidence_status is '#747: verified = every consumed observation dereferences to a raw fetch and every asserted input names its evidence; degraded = an asserted input has no evidence on the row (e.g. a seed headcount); rejected = a pointer does not resolve.';
comment on column mart.topt_core_results.factor_validation_status is '#747 / #65: the factor definitions'' independent holdout verdict from factors.validation_records; not_evaluated until a sealed record exists.';
comment on column mart.topt_gppe_results.availability_status is '#747 / init.md §8: availability of this row (contracts AvailabilityStatus); see mart.topt_core_results.availability_status.';
comment on column mart.topt_gppe_results.source_evidence_status is '#747: verified / degraded / rejected — see mart.topt_core_results.source_evidence_status.';
comment on column mart.topt_gppe_results.factor_validation_status is '#747 / #65: accepted / rejected / not_evaluated from factors.validation_records — see mart.topt_core_results.factor_validation_status.';
comment on column mart.strategy_decisions.availability_status is '#747 / init.md §8: availability of this row (contracts AvailabilityStatus); see mart.topt_core_results.availability_status.';
comment on column mart.strategy_decisions.source_evidence_status is '#747: verified / degraded / rejected — see mart.topt_core_results.source_evidence_status.';
comment on column mart.strategy_decisions.factor_validation_status is '#747 / #65: accepted / rejected / not_evaluated from factors.validation_records — see mart.topt_core_results.factor_validation_status.';
