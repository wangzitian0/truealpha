-- #1730 Slice 1: remove redundant indexes and add hot-path and FK coverage.

-- 1. Drop redundant indexes (guarded by to_regclass so replay takes zero locks).
do $$ begin if to_regclass('mart.idx_backtest_val_run_res') is not null then drop index if exists mart.idx_backtest_val_run_res; end if; end $$;
do $$ begin if to_regclass('mart.idx_mart_cell_confidence_run_subject') is not null then drop index if exists mart.idx_mart_cell_confidence_run_subject; end if; end $$;
do $$ begin if to_regclass('mart.idx_strategy_decisions_run') is not null then drop index if exists mart.idx_strategy_decisions_run; end if; end $$;
do $$ begin if to_regclass('staging.idx_entity_aliases_lookup') is not null then drop index if exists staging.idx_entity_aliases_lookup; end if; end $$;
do $$ begin if to_regclass('staging.idx_kg_identifiers_asof') is not null then drop index if exists staging.idx_kg_identifiers_asof; end if; end $$;

-- 2. Add hot-path and foreign-key indexes on mart.
do $$ begin if to_regclass('mart.topt_gppe_results') is not null and to_regclass('mart.idx_topt_gppe_results_lookup') is null then CREATE INDEX idx_topt_gppe_results_lookup ON mart.topt_gppe_results USING btree (run_id, issuer_id, cutoff); end if; end $$;
do $$ begin if to_regclass('mart.topt_gppe_results') is not null and to_regclass('mart.idx_topt_gppe_results_snapshot') is null then CREATE INDEX idx_topt_gppe_results_snapshot ON mart.topt_gppe_results USING btree (snapshot_id); end if; end $$;
do $$ begin if to_regclass('mart.topt_core_results') is not null and to_regclass('mart.idx_topt_core_results_snapshot') is null then CREATE INDEX idx_topt_core_results_snapshot ON mart.topt_core_results USING btree (snapshot_id); end if; end $$;
do $$ begin if to_regclass('mart.topt_core_results') is not null and to_regclass('mart.idx_topt_core_results_gppe_result') is null then CREATE INDEX idx_topt_core_results_gppe_result ON mart.topt_core_results USING btree (gppe_result_id); end if; end $$;
do $$ begin if to_regclass('mart.topt_core_results') is not null and to_regclass('mart.idx_topt_core_results_gppe_invocation') is null then CREATE INDEX idx_topt_core_results_gppe_invocation ON mart.topt_core_results USING btree (gppe_invocation_id); end if; end $$;
do $$ begin if to_regclass('mart.topt_core_invocations') is not null and to_regclass('mart.idx_topt_core_invocations_gppe_invocation') is null then CREATE INDEX idx_topt_core_invocations_gppe_invocation ON mart.topt_core_invocations USING btree (gppe_invocation_id); end if; end $$;
do $$ begin if to_regclass('mart.idx_current_pointer_target_run') is null then CREATE INDEX idx_current_pointer_target_run ON mart.current_pointer USING btree (target_run_id); end if; end $$;
do $$ begin if to_regclass('mart.idx_current_pointer_previous_run') is null then CREATE INDEX idx_current_pointer_previous_run ON mart.current_pointer USING btree (previous_run_id) WHERE (previous_run_id IS NOT NULL); end if; end $$;

-- 3. Add hot-path and foreign-key indexes on staging.
do $$ begin if to_regclass('staging.idx_capture_obs_obligation') is null then CREATE INDEX idx_capture_obs_obligation ON staging.capture_normalized_observations USING btree (capture_obligation_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_capture_obs_vintage') is null then CREATE INDEX idx_capture_obs_vintage ON staging.capture_normalized_observations USING btree (source_vintage_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_capture_obs_supersedes') is null then CREATE INDEX idx_capture_obs_supersedes ON staging.capture_normalized_observations USING btree (supersedes_observation_id) WHERE (supersedes_observation_id IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('staging.idx_capture_obs_subject_pit') is null then CREATE INDEX idx_capture_obs_subject_pit ON staging.capture_normalized_observations USING btree (subject_kind, subject_id, semantic_type, knowable_at DESC); end if; end $$;
do $$ begin if to_regclass('staging.idx_capture_obs_obligations_obs') is null then CREATE INDEX idx_capture_obs_obligations_obs ON staging.capture_observation_obligations USING btree (observation_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_etf_constituent_facts_raw_fetch') is null then CREATE INDEX idx_etf_constituent_facts_raw_fetch ON staging.etf_constituent_facts USING btree (raw_fetch_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_landed_fetch_keys_raw_fetch') is null then CREATE INDEX idx_landed_fetch_keys_raw_fetch ON staging.landed_fetch_keys USING btree (raw_fetch_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_evidence_edges_from_id') is null then CREATE INDEX idx_evidence_edges_from_id ON staging.evidence_edges USING btree (from_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_evidence_edges_to_id') is null then CREATE INDEX idx_evidence_edges_to_id ON staging.evidence_edges USING btree (to_id); end if; end $$;
do $$ begin if to_regclass('staging.idx_evidence_nodes_supersedes') is null then CREATE INDEX idx_evidence_nodes_supersedes ON staging.evidence_nodes USING btree (supersedes_node_id) WHERE (supersedes_node_id IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('staging.idx_fund_holding_facts_holding') is null then CREATE INDEX idx_fund_holding_facts_holding ON staging.fund_holding_facts USING btree (holding_id) WHERE (holding_id IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('staging.kg_edges') is not null and to_regclass('staging.idx_kg_edges_to') is null then CREATE INDEX idx_kg_edges_to ON staging.kg_edges USING btree (to_id); end if; end $$;

-- 4. Add foreign-key indexes on raw.
do $$ begin if to_regclass('raw.idx_capture_vintages_fetch') is null then CREATE INDEX idx_capture_vintages_fetch ON raw.capture_source_vintages USING btree (raw_fetch_id); end if; end $$;
do $$ begin if to_regclass('raw.idx_capture_vintages_request') is null then CREATE INDEX idx_capture_vintages_request ON raw.capture_source_vintages USING btree (source_request_id); end if; end $$;
do $$ begin if to_regclass('raw.idx_capture_attempt_results_vintage') is null then CREATE INDEX idx_capture_attempt_results_vintage ON raw.capture_attempt_results USING btree (source_vintage_id); end if; end $$;
do $$ begin if to_regclass('raw.idx_capture_attempt_results_reused_vintage') is null then CREATE INDEX idx_capture_attempt_results_reused_vintage ON raw.capture_attempt_results USING btree (reused_source_vintage_id) WHERE (reused_source_vintage_id IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('raw.idx_capture_obligation_results_attempt') is null then CREATE INDEX idx_capture_obligation_results_attempt ON raw.capture_obligation_results USING btree (final_attempt_id) WHERE (final_attempt_id IS NOT NULL); end if; end $$;
do $$ begin if to_regclass('raw.idx_capture_work_items_request') is null then CREATE INDEX idx_capture_work_items_request ON raw.capture_work_items USING btree (source_request_id); end if; end $$;
do $$ begin if to_regclass('raw.idx_capture_obligation_work_bindings_work_item') is null then CREATE INDEX idx_capture_obligation_work_bindings_work_item ON raw.capture_obligation_work_bindings USING btree (work_item_id); end if; end $$;
do $$ begin if to_regclass('raw.idx_capture_obligations_campaign_list') is null then CREATE INDEX idx_capture_obligations_campaign_list ON raw.capture_obligations USING btree (campaign_id, list_version_id); end if; end $$;
do $$ begin if to_regclass('raw.idx_capture_obligations_list_subject') is null then CREATE INDEX idx_capture_obligations_list_subject ON raw.capture_obligations USING btree (list_version_id, subject_kind, subject_id); end if; end $$;
do $$ begin if to_regclass('raw.idx_capture_obligations_run_campaign') is null then CREATE INDEX idx_capture_obligations_run_campaign ON raw.capture_obligations USING btree (run_id, campaign_id); end if; end $$;
do $$ begin if to_regclass('raw.idx_capture_campaign_list_versions_list') is null then CREATE INDEX idx_capture_campaign_list_versions_list ON raw.capture_campaign_list_versions USING btree (list_version_id); end if; end $$;
do $$ begin if to_regclass('raw.idx_capture_runs_schedule_policy') is null then CREATE INDEX idx_capture_runs_schedule_policy ON raw.capture_runs USING btree (schedule_policy_id); end if; end $$;

-- 5. Add foreign-key indexes on app.
do $$ begin if to_regclass('app.idx_principals_tenant') is null then CREATE INDEX idx_principals_tenant ON app.principals USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_conversations_tenant') is null then CREATE INDEX idx_conversations_tenant ON app.conversations USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_conversations_owner') is null then CREATE INDEX idx_conversations_owner ON app.conversations USING btree (owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_conversation_messages_conv_tenant_owner') is null then CREATE INDEX idx_conversation_messages_conv_tenant_owner ON app.conversation_messages USING btree (conversation_id, tenant_id, owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_conversation_messages_tenant') is null then CREATE INDEX idx_conversation_messages_tenant ON app.conversation_messages USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_conversation_messages_owner') is null then CREATE INDEX idx_conversation_messages_owner ON app.conversation_messages USING btree (owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_entitlement_grants_tenant') is null then CREATE INDEX idx_entitlement_grants_tenant ON app.entitlement_grants USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_entitlement_grants_principal') is null then CREATE INDEX idx_entitlement_grants_principal ON app.entitlement_grants USING btree (principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_documents_tenant') is null then CREATE INDEX idx_research_documents_tenant ON app.research_documents USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_documents_owner') is null then CREATE INDEX idx_research_documents_owner ON app.research_documents USING btree (owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_revisions_doc_tenant_owner') is null then CREATE INDEX idx_research_document_revisions_doc_tenant_owner ON app.research_document_revisions USING btree (document_id, tenant_id, owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_revisions_tenant') is null then CREATE INDEX idx_research_document_revisions_tenant ON app.research_document_revisions USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_revisions_owner') is null then CREATE INDEX idx_research_document_revisions_owner ON app.research_document_revisions USING btree (owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_tombstones_tenant') is null then CREATE INDEX idx_research_document_tombstones_tenant ON app.research_document_tombstones USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_tombstones_owner') is null then CREATE INDEX idx_research_document_tombstones_owner ON app.research_document_tombstones USING btree (owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_download_tickets_doc_tenant_owner') is null then CREATE INDEX idx_research_document_download_tickets_doc_tenant_owner ON app.research_document_download_tickets USING btree (document_id, tenant_id, owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_download_tickets_rev_doc_tenant_owner') is null then CREATE INDEX idx_research_document_download_tickets_rev_doc_tenant_owner ON app.research_document_download_tickets USING btree (revision_id, document_id, tenant_id, owner_principal_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_download_tickets_tenant') is null then CREATE INDEX idx_research_document_download_tickets_tenant ON app.research_document_download_tickets USING btree (tenant_id); end if; end $$;
do $$ begin if to_regclass('app.idx_research_document_download_tickets_owner') is null then CREATE INDEX idx_research_document_download_tickets_owner ON app.research_document_download_tickets USING btree (owner_principal_id); end if; end $$;
