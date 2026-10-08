-- #1108: SSOT convergence: factor and valuation evidence chain guards.
--
-- Re-asserts calibrated confidence bounds across staging capture observations and backtest inputs.
-- Safe to replay on every boot.

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'staging.capture_normalized_observations'::regclass
          and conname = 'capture_obs_confidence_bounded'
    ) then
        alter table staging.capture_normalized_observations
            add constraint capture_obs_confidence_bounded check (confidence >= 0 and confidence <= 1);
    end if;
end
$$;
