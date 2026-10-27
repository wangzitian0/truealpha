-- #1521 step 3: staging.api_call_ledger names why a repeat of a fetch key is not a duplicate fetch.
--
--   repeat_reason  the reason that the source gateway derives from the call itself, never from an
--                  argument of the caller:
--                    probe             a forced run (force_fetch) asks the vendor again on purpose
--                    window_not_final  the request is not final, so the next caller asks again
--                    answer_refused    the registered accept rule of the key refused the answer
--                    model_call        a model attempt; the same request can be asked again
--                  Null for an ordinary call. The nightly verdict fetch_keys_once counts rows per key and
--                  skips every row that carries a reason.
--
-- Backward compatible and replay-safe: a nullable add, guarded so a replay on a settled table takes no
-- ACCESS EXCLUSIVE lock (boot-lock guard, 2026-09-17). The first application takes a table lock on the ledger:
-- it follows the release that moved every ledger row to the autocommit writer (#1524), so no caller holds a
-- row lock on the table until its transaction ends. No CHECK and no NOT NULL: the gateway is the one writer.

do $$
begin
    if not exists (
        select 1 from pg_attribute
        where attrelid = 'staging.api_call_ledger'::regclass and attname = 'repeat_reason' and not attisdropped
    ) then
        alter table staging.api_call_ledger add column if not exists repeat_reason text;
    end if;
end
$$;

comment on column staging.api_call_ledger.repeat_reason is
    '#1521: why a repeat of fetch_key is not a duplicate fetch (probe, window_not_final, answer_refused, model_call); null for an ordinary call. Derived by the gateway.';
