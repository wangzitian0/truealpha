-- #70 / #735 (init.md §9): the provider routes legacy model names to its current
-- models — every production invocation on 2026-09-07 asked for glm-4.7 and the response
-- said glm-5.3-flash. `model` stays what we asked for (it is part of the request digest
-- and of the invocation identity); `served_model` is what the provider reports it
-- answered with, and it is the model named in the fact's extractor id.
alter table staging.model_invocations
    add column if not exists served_model text;
comment on column staging.model_invocations.served_model is
    'The model the provider reports having answered with (response.model); null when the vendor errored or did not say. `model` is the requested name.';
