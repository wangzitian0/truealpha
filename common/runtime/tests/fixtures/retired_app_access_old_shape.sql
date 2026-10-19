-- Test fixture for #1240: the seven retired app.* access tables, their view and their six functions.
-- It holds the shape that the migration chain of commit 344e681 created.
-- Source: pg_dump --schema-only and pg_get_functiondef on a database that ran that chain.
-- Apply it to a database that ran the current chain.
-- The result has the shape of a deployed Staging or Production database before the retire migration.
-- common/runtime/tests/test_migration_retire_app_access_tables.py is the only user.

SET check_function_bodies = false;

CREATE OR REPLACE FUNCTION app.validate_access_audit_decision_tenant()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'app'
AS $function$
declare
    decision_record app.authorization_decisions%rowtype;
    expected_event_kind text;
begin
    select * into decision_record
    from app.authorization_decisions
    where decision_id = new.decision_id;

    if decision_record.decision_id is null then
        raise exception 'access audit decision does not exist';
    end if;
    if decision_record.tenant_id is distinct from new.tenant_id then
        raise exception 'access audit tenant must match its authorization decision';
    end if;
    if decision_record.principal_id is distinct from new.principal_id then
        raise exception 'access audit principal must match its authorization decision';
    end if;
    if decision_record.decided_at <> new.occurred_at then
        raise exception 'access audit time must match its authorization decision';
    end if;
    expected_event_kind := case
        when decision_record.decision = 'allow' then 'access_allowed'
        when decision_record.reason_code in (
            'authentication_missing',
            'authentication_invalid',
            'authentication_not_yet_valid',
            'authentication_expired',
            'delegation_revoked',
            'client_authority_claim_rejected'
        ) then 'authentication_denied'
        else 'access_denied'
    end;
    if new.event_kind <> expected_event_kind then
        raise exception 'access audit event kind must match its authorization decision';
    end if;
    return new;
end;
$function$

;

CREATE OR REPLACE FUNCTION app.validate_publication_policy_entitlement_insert()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'app'
AS $function$
begin
    perform 1
    from app.publication_policy_sets
    where publication_policy_set_id = new.publication_policy_set_id
    for update;

    if exists (
        select 1
        from app.publication_policy_set_seals as seal
        where seal.publication_policy_set_id = new.publication_policy_set_id
    ) then
        raise exception 'sealed publication policy set cannot accept new rules';
    end if;
    return new;
end;
$function$

;

CREATE OR REPLACE FUNCTION app.validate_publication_policy_set_seal()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'app'
AS $function$
declare
    policy_content_sha256 text;
    policy_administrator_actions text[];
begin
    select content_sha256, administrator_actions
    into policy_content_sha256, policy_administrator_actions
    from app.publication_policy_sets
    where publication_policy_set_id = new.publication_policy_set_id
    for update;

    if policy_content_sha256 is null then
        raise exception 'publication policy set does not exist';
    end if;
    if policy_content_sha256 <> new.content_sha256 then
        raise exception 'publication policy set seal content hash mismatch';
    end if;
    if cardinality(policy_administrator_actions) <> (
        select count(distinct administrator_action)
        from unnest(policy_administrator_actions) as administrator_action
    ) then
        raise exception 'publication policy set administrator actions must be unique';
    end if;
    if not exists (
        select 1
        from app.publication_policy_entitlements as policy_rule
        where policy_rule.publication_policy_set_id = new.publication_policy_set_id
    ) then
        raise exception 'publication policy set cannot be sealed without rules';
    end if;
    return new;
end;
$function$

;

CREATE OR REPLACE FUNCTION app.validate_authorization_decision_policy_set()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'app'
AS $function$
begin
    if new.resource_type is null then
        if new.publication_class_id is not null then
            raise exception 'legacy authorization decision cannot carry a publication class';
        end if;
        return new;
    end if;
    if new.resource_type not in (
        'private_conversation',
        'private_document',
        'materialized_strategy_result',
        'materialized_backtest_result',
        'access_audit_metadata',
        'registered_replay_definition'
    ) then
        raise exception 'authorization decision resource type is invalid';
    end if;
    if (new.resource_type in ('materialized_strategy_result', 'materialized_backtest_result'))
       is distinct from (new.publication_class_id is not null) then
        raise exception 'authorization decision publication class shape is invalid';
    end if;
    if not exists (
        select 1
        from app.publication_policy_sets as policy_set
        join app.publication_policy_set_seals as seal
          on seal.publication_policy_set_id = policy_set.publication_policy_set_id
         and seal.content_sha256 = policy_set.content_sha256
        where policy_set.publication_policy_set_id = new.publication_policy_id
    ) then
        raise exception 'authorization decision policy set is missing or unsealed';
    end if;
    return new;
end;
$function$

;

CREATE OR REPLACE FUNCTION app.validate_authorization_decision_grant()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'app'
AS $function$
declare
    decision_record app.authorization_decisions%rowtype;
    grant_record app.entitlement_grants%rowtype;
begin
    select * into decision_record
    from app.authorization_decisions
    where decision_id = new.decision_id;

    select * into grant_record
    from app.entitlement_grants
    where grant_id = new.grant_id;

    if decision_record.decision_id is null or grant_record.grant_id is null then
        raise exception 'authorization decision or entitlement grant does not exist';
    end if;
    if decision_record.decision <> 'allow' then
        raise exception 'denied authorization decision cannot claim an entitlement grant';
    end if;
    if decision_record.action <> 'read_materialized_result'
       or decision_record.resource_type not in (
           'materialized_strategy_result',
           'materialized_backtest_result'
       ) then
        raise exception 'only materialized-result decisions can claim an entitlement grant';
    end if;
    if decision_record.tenant_id is distinct from grant_record.tenant_id
       or decision_record.principal_id is distinct from grant_record.principal_id then
        raise exception 'authorization decision grant identity mismatch';
    end if;
    if decision_record.publication_policy_id <> grant_record.publication_policy_id then
        raise exception 'authorization decision grant policy mismatch';
    end if;
    if not exists (
        select 1
        from app.publication_policy_entitlements as policy_rule
        where policy_rule.publication_policy_set_id = decision_record.publication_policy_id
          and policy_rule.publication_class_id = decision_record.publication_class_id
          and policy_rule.entitlement_id = grant_record.entitlement_id
    ) then
        raise exception 'authorization decision grant entitlement is not permitted';
    end if;
    if decision_record.decided_at < grant_record.valid_from
       or (grant_record.valid_until is not null and decision_record.decided_at >= grant_record.valid_until)
       or exists (
           select 1
           from app.grant_revocations as revocation
           where revocation.grant_id = grant_record.grant_id
             and revocation.revoked_at <= decision_record.decided_at
       ) then
        raise exception 'authorization decision grant was not active at decision time';
    end if;
    return new;
end;
$function$

;

CREATE OR REPLACE FUNCTION app.validate_authorization_decision_required_grants()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'app'
AS $function$
begin
    if new.decision = 'allow'
       and new.resource_type in (
           'materialized_strategy_result',
           'materialized_backtest_result'
       )
       and not exists (
           select 1
           from app.principals as principal
           where principal.principal_id = new.principal_id
             and principal.principal_kind = 'administrator'
       )
       and not exists (
           select 1
           from app.authorization_decision_grants as decision_grant
           where decision_grant.decision_id = new.decision_id
       ) then
        raise exception 'non-administrator materialized allow decision requires an entitlement grant';
    end if;
    return new;
end;
$function$

;

SET search_path = '';

CREATE TABLE app.access_audit_events (
    audit_event_id text NOT NULL,
    decision_id text NOT NULL,
    tenant_id text,
    principal_id text,
    event_kind text NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT access_audit_events_audit_event_id_check CHECK ((length(audit_event_id) > 0)),
    CONSTRAINT access_audit_events_check CHECK ((recorded_at >= occurred_at)),
    CONSTRAINT access_audit_events_event_kind_check CHECK ((event_kind = ANY (ARRAY['access_allowed'::text, 'access_denied'::text, 'authentication_denied'::text])))
);

CREATE VIEW app.access_audit_metadata AS
SELECT
    NULL::text AS audit_event_id,
    NULL::text AS decision_id,
    NULL::text AS tenant_id,
    NULL::text AS principal_id,
    NULL::text AS action,
    NULL::text AS resource_id,
    NULL::text AS publication_policy_id,
    NULL::text AS decision,
    NULL::text AS reason_code,
    NULL::text AS event_kind,
    NULL::timestamp with time zone AS occurred_at,
    NULL::timestamp with time zone AS recorded_at,
    NULL::text AS publication_policy_content_sha256,
    NULL::text AS release_manifest_id,
    NULL::text AS resource_type,
    NULL::text AS publication_class_id,
    NULL::text[] AS entitlement_grant_ids;

CREATE TABLE app.authorization_decision_grants (
    decision_id text NOT NULL,
    grant_id text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE app.authorization_decisions (
    decision_id text NOT NULL,
    tenant_id text,
    principal_id text,
    action text NOT NULL,
    resource_id text NOT NULL,
    publication_policy_id text NOT NULL,
    decision text NOT NULL,
    reason_code text,
    decided_at timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    resource_type text,
    publication_class_id text,
    CONSTRAINT authorization_decisions_action_check CHECK ((length(action) > 0)),
    CONSTRAINT authorization_decisions_check CHECK ((((decision = 'allow'::text) AND (reason_code IS NULL)) OR ((decision = 'deny'::text) AND (reason_code IS NOT NULL)))),
    CONSTRAINT authorization_decisions_check1 CHECK ((recorded_at >= decided_at)),
    CONSTRAINT authorization_decisions_decision_check CHECK ((decision = ANY (ARRAY['allow'::text, 'deny'::text]))),
    CONSTRAINT authorization_decisions_decision_id_check CHECK ((decision_id ~ '^access-decision:[0-9a-f]{64}$'::text)),
    CONSTRAINT authorization_decisions_publication_policy_id_check CHECK ((length(publication_policy_id) > 0)),
    CONSTRAINT authorization_decisions_resource_id_check CHECK ((length(resource_id) > 0))
);

CREATE TABLE app.grant_revocations (
    revocation_id text NOT NULL,
    tenant_id text NOT NULL,
    grant_id text NOT NULL,
    revoked_at timestamp with time zone NOT NULL,
    reason_code text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT grant_revocations_check CHECK ((recorded_at >= revoked_at)),
    CONSTRAINT grant_revocations_reason_code_check CHECK ((length(reason_code) > 0)),
    CONSTRAINT grant_revocations_revocation_id_check CHECK ((length(revocation_id) > 0))
);

CREATE TABLE app.publication_policy_entitlements (
    publication_policy_rule_id text NOT NULL,
    publication_policy_set_id text NOT NULL,
    publication_class_id text NOT NULL,
    entitlement_id text NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT publication_policy_entitlement_publication_policy_rule_id_check CHECK ((length(publication_policy_rule_id) > 0)),
    CONSTRAINT publication_policy_entitlements_entitlement_id_check CHECK ((length(entitlement_id) > 0)),
    CONSTRAINT publication_policy_entitlements_publication_class_id_check CHECK ((length(publication_class_id) > 0))
);

CREATE TABLE app.publication_policy_set_seals (
    publication_policy_set_id text NOT NULL,
    content_sha256 text NOT NULL,
    sealed_at timestamp with time zone NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT publication_policy_set_seals_check CHECK ((recorded_at >= sealed_at)),
    CONSTRAINT publication_policy_set_seals_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text))
);

CREATE TABLE app.publication_policy_sets (
    publication_policy_set_id text NOT NULL,
    content_sha256 text NOT NULL,
    release_manifest_id text NOT NULL,
    administrator_actions text[] DEFAULT ARRAY[]::text[] NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT publication_policy_sets_administrator_actions_check CHECK (((array_position(administrator_actions, NULL::text) IS NULL) AND (administrator_actions <@ ARRAY['read_audit_metadata'::text, 'submit_registered_replay'::text]))),
    CONSTRAINT publication_policy_sets_content_sha256_check CHECK ((content_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT publication_policy_sets_publication_policy_set_id_check CHECK ((length(publication_policy_set_id) > 0)),
    CONSTRAINT publication_policy_sets_release_manifest_id_check CHECK ((length(release_manifest_id) > 0))
);

ALTER TABLE app.access_audit_events
    ADD CONSTRAINT access_audit_events_content_id_check CHECK ((audit_event_id ~ '^access-audit-event:[0-9a-f]{64}$'::text)) NOT VALID;

ALTER TABLE ONLY app.access_audit_events
    ADD CONSTRAINT access_audit_events_pkey PRIMARY KEY (audit_event_id);

ALTER TABLE ONLY app.authorization_decision_grants
    ADD CONSTRAINT authorization_decision_grants_pkey PRIMARY KEY (decision_id, grant_id);

ALTER TABLE ONLY app.authorization_decisions
    ADD CONSTRAINT authorization_decisions_pkey PRIMARY KEY (decision_id);

ALTER TABLE ONLY app.grant_revocations
    ADD CONSTRAINT grant_revocations_pkey PRIMARY KEY (revocation_id);

ALTER TABLE ONLY app.publication_policy_entitlements
    ADD CONSTRAINT publication_policy_entitlemen_publication_policy_set_id_pub_key UNIQUE (publication_policy_set_id, publication_class_id, entitlement_id);

ALTER TABLE ONLY app.publication_policy_entitlements
    ADD CONSTRAINT publication_policy_entitlements_pkey PRIMARY KEY (publication_policy_rule_id);

ALTER TABLE ONLY app.publication_policy_set_seals
    ADD CONSTRAINT publication_policy_set_seals_pkey PRIMARY KEY (publication_policy_set_id);

ALTER TABLE ONLY app.publication_policy_sets
    ADD CONSTRAINT publication_policy_sets_pkey PRIMARY KEY (publication_policy_set_id);

CREATE OR REPLACE VIEW app.access_audit_metadata WITH (security_barrier='true') AS
 SELECT event.audit_event_id,
    event.decision_id,
    event.tenant_id,
    event.principal_id,
    decision.action,
        CASE
            WHEN (decision.resource_type = ANY (ARRAY['private_conversation'::text, 'private_document'::text])) THEN NULL::text
            ELSE decision.resource_id
        END AS resource_id,
    decision.publication_policy_id,
    decision.decision,
    decision.reason_code,
    event.event_kind,
    event.occurred_at,
    event.recorded_at,
    policy_set.content_sha256 AS publication_policy_content_sha256,
    policy_set.release_manifest_id,
    decision.resource_type,
    decision.publication_class_id,
    COALESCE(array_agg(decision_grant.grant_id ORDER BY decision_grant.grant_id) FILTER (WHERE (decision_grant.grant_id IS NOT NULL)), ARRAY[]::text[]) AS entitlement_grant_ids
   FROM (((app.access_audit_events event
     JOIN app.authorization_decisions decision ON (((decision.decision_id = event.decision_id) AND (NOT (decision.tenant_id IS DISTINCT FROM event.tenant_id)))))
     LEFT JOIN app.publication_policy_sets policy_set ON ((policy_set.publication_policy_set_id = decision.publication_policy_id)))
     LEFT JOIN app.authorization_decision_grants decision_grant ON ((decision_grant.decision_id = decision.decision_id)))
  WHERE ((event.tenant_id = NULLIF(current_setting('truealpha.tenant_id'::text, true), ''::text)) AND (EXISTS ( SELECT 1
           FROM app.principals reader
          WHERE ((reader.principal_id = NULLIF(current_setting('truealpha.principal_id'::text, true), ''::text)) AND (reader.principal_kind = 'administrator'::text)))))
  GROUP BY event.audit_event_id, decision.decision_id, policy_set.publication_policy_set_id, policy_set.content_sha256, policy_set.release_manifest_id;

CREATE TRIGGER trg_access_audit_events_append_only BEFORE DELETE OR UPDATE ON app.access_audit_events FOR EACH ROW EXECUTE FUNCTION app.reject_mutation();

CREATE TRIGGER trg_access_audit_events_validate_tenant BEFORE INSERT ON app.access_audit_events FOR EACH ROW EXECUTE FUNCTION app.validate_access_audit_decision_tenant();

CREATE TRIGGER trg_authorization_decision_grants_append_only BEFORE DELETE OR UPDATE ON app.authorization_decision_grants FOR EACH ROW EXECUTE FUNCTION app.reject_mutation();

CREATE TRIGGER trg_authorization_decision_grants_validate BEFORE INSERT ON app.authorization_decision_grants FOR EACH ROW EXECUTE FUNCTION app.validate_authorization_decision_grant();

CREATE TRIGGER trg_authorization_decisions_append_only BEFORE DELETE OR UPDATE ON app.authorization_decisions FOR EACH ROW EXECUTE FUNCTION app.reject_mutation();

CREATE CONSTRAINT TRIGGER trg_authorization_decisions_require_grants AFTER INSERT ON app.authorization_decisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.validate_authorization_decision_required_grants();

CREATE TRIGGER trg_authorization_decisions_validate_policy_set BEFORE INSERT ON app.authorization_decisions FOR EACH ROW EXECUTE FUNCTION app.validate_authorization_decision_policy_set();

CREATE TRIGGER trg_grant_revocations_append_only BEFORE DELETE OR UPDATE ON app.grant_revocations FOR EACH ROW EXECUTE FUNCTION app.reject_mutation();

CREATE TRIGGER trg_publication_policy_entitlements_append_only BEFORE DELETE OR UPDATE ON app.publication_policy_entitlements FOR EACH ROW EXECUTE FUNCTION app.reject_mutation();

CREATE TRIGGER trg_publication_policy_entitlements_validate_insert BEFORE INSERT ON app.publication_policy_entitlements FOR EACH ROW EXECUTE FUNCTION app.validate_publication_policy_entitlement_insert();

CREATE TRIGGER trg_publication_policy_set_seals_append_only BEFORE DELETE OR UPDATE ON app.publication_policy_set_seals FOR EACH ROW EXECUTE FUNCTION app.reject_mutation();

CREATE TRIGGER trg_publication_policy_set_seals_validate BEFORE INSERT ON app.publication_policy_set_seals FOR EACH ROW EXECUTE FUNCTION app.validate_publication_policy_set_seal();

CREATE TRIGGER trg_publication_policy_sets_append_only BEFORE DELETE OR UPDATE ON app.publication_policy_sets FOR EACH ROW EXECUTE FUNCTION app.reject_mutation();

ALTER TABLE ONLY app.access_audit_events
    ADD CONSTRAINT access_audit_events_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES app.authorization_decisions(decision_id);

ALTER TABLE ONLY app.authorization_decision_grants
    ADD CONSTRAINT authorization_decision_grants_decision_id_fkey FOREIGN KEY (decision_id) REFERENCES app.authorization_decisions(decision_id);

ALTER TABLE ONLY app.authorization_decision_grants
    ADD CONSTRAINT authorization_decision_grants_grant_id_fkey FOREIGN KEY (grant_id) REFERENCES app.entitlement_grants(grant_id);

ALTER TABLE ONLY app.grant_revocations
    ADD CONSTRAINT grant_revocations_grant_id_fkey FOREIGN KEY (grant_id) REFERENCES app.entitlement_grants(grant_id);

ALTER TABLE ONLY app.grant_revocations
    ADD CONSTRAINT grant_revocations_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES app.tenants(tenant_id);

ALTER TABLE ONLY app.publication_policy_entitlements
    ADD CONSTRAINT publication_policy_entitlements_publication_policy_set_id_fkey FOREIGN KEY (publication_policy_set_id) REFERENCES app.publication_policy_sets(publication_policy_set_id);

ALTER TABLE ONLY app.publication_policy_set_seals
    ADD CONSTRAINT publication_policy_set_seals_publication_policy_set_id_fkey FOREIGN KEY (publication_policy_set_id) REFERENCES app.publication_policy_sets(publication_policy_set_id);
