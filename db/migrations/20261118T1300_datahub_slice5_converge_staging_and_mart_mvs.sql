-- Retire write-only staging.analyst_rating_events and convert heavy mart views
-- (entity_identity, fund_holdings, fund_holdings_resolved, fund_holdings_coverage,
-- fund_holdings_valuation) to materialized views with UNIQUE INDEX ... NULLS NOT DISTINCT
-- for non-blocking REFRESH MATERIALIZED VIEW CONCURRENTLY (#1730 Slice 5).
do $$
declare
    v_rel text;
    v_kind "char";
    v_entity_is_mv boolean := exists (
        select 1
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'mart' and c.relname = 'entity_identity' and c.relkind = 'm'
    );
    v_holdings_is_mv boolean := (
        select count(*) = 4
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'mart'
          and c.relname in ('fund_holdings', 'fund_holdings_resolved', 'fund_holdings_coverage', 'fund_holdings_valuation')
          and c.relkind = 'm'
    );
begin
    -- 1. Retire write-only duplicate table staging.analyst_rating_events.
    drop table if exists staging.analyst_rating_events;

    -- 2. Convert mart.entity_identity view -> materialized view.
    if not v_entity_is_mv then
        drop view if exists mart.entity_identity;
        execute $mv$
create materialized view mart.entity_identity as
 WITH RECURSIVE merge_edges AS (
         SELECT DISTINCT ON (relation.from_entity_id) relation.from_entity_id,
            relation.to_entity_id
           FROM staging.entity_relations relation
          WHERE relation.relation_type = 'superseded_by'::text AND NOT (EXISTS ( SELECT 1
                   FROM staging.entity_retractions retraction
                  WHERE retraction.relation_id = relation.relation_id))
          ORDER BY relation.from_entity_id, relation.transaction_time DESC, relation.relation_id DESC
        ), survivor_walk(entity_id, current_id, depth) AS (
         SELECT entity.entity_id,
            entity.entity_id,
            0 AS "?column?"
           FROM staging.entities entity
        UNION ALL
         SELECT walk.entity_id,
            edge.to_entity_id,
            walk.depth + 1
           FROM survivor_walk walk
             JOIN merge_edges edge ON edge.from_entity_id = walk.current_id
          WHERE walk.depth < 32
        ), survivors AS (
         SELECT DISTINCT ON (walk.entity_id) walk.entity_id,
            walk.current_id AS id
           FROM survivor_walk walk
          ORDER BY walk.entity_id, walk.depth DESC
        ), alias_retractions AS (
         SELECT retraction.alias_id,
            min(retraction.valid_to) AS valid_to
           FROM staging.entity_retractions retraction
          WHERE retraction.alias_id IS NOT NULL
          GROUP BY retraction.alias_id
        ), active_aliases AS (
         SELECT survivor_1.id AS survivor_id,
            alias.scheme,
            alias.value,
            row_number() OVER (PARTITION BY survivor_1.id, alias.scheme ORDER BY alias.confidence DESC, alias.transaction_time DESC, alias.alias_id DESC) AS rn
           FROM staging.entity_aliases alias
             JOIN survivors survivor_1 ON survivor_1.entity_id = alias.entity_id
             LEFT JOIN alias_retractions retracted ON retracted.alias_id = alias.alias_id
          WHERE LEAST(alias.valid_to, retracted.valid_to) IS NULL OR LEAST(alias.valid_to, retracted.valid_to) > alias.valid_from
        ), pivoted_aliases AS MATERIALIZED (
         SELECT active_aliases.survivor_id,
            max(active_aliases.value) FILTER (WHERE active_aliases.scheme = 'cik'::text) AS cik,
            max(active_aliases.value) FILTER (WHERE active_aliases.scheme = 'lei'::text) AS lei,
            max(active_aliases.value) FILTER (WHERE active_aliases.scheme = 'name'::text) AS name,
            max(active_aliases.value) FILTER (WHERE active_aliases.scheme = 'legacy-id'::text) AS legacy_id,
            COALESCE(
                max(split_part(active_aliases.value, ':'::text, 2)) FILTER (WHERE active_aliases.scheme = 'mic-ticker'::text),
                max(upper(split_part(active_aliases.value, ':'::text, 3))) FILTER (
                    WHERE active_aliases.scheme = 'legacy-id'::text
                      AND active_aliases.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text
                ),
                max(upper(active_aliases.value)) FILTER (WHERE active_aliases.scheme = 'ticker'::text)
            ) AS direct_ticker,
            CASE
                WHEN bool_or(active_aliases.scheme = 'legacy-id'::text) THEN max(
                    CASE
                        WHEN active_aliases.scheme = 'legacy-id'::text
                         AND active_aliases.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text
                        THEN active_aliases.value
                    END
                )
                ELSE max(
                    CASE
                        WHEN active_aliases.scheme = 'mic-ticker'::text
                        THEN (('listing:'::text || lower(split_part(active_aliases.value, ':'::text, 1))) || ':'::text) || lower(split_part(active_aliases.value, ':'::text, 2))
                    END
                )
            END AS direct_listing
           FROM active_aliases
          WHERE active_aliases.rn = 1
          GROUP BY active_aliases.survivor_id
        ), primary_listings AS (
         SELECT DISTINCT ON (relation.from_entity_id) relation.from_entity_id AS issuer_id,
            relation.to_entity_id AS listing_id
           FROM staging.entity_relations relation
          WHERE relation.relation_type = 'primary_listing'::text AND NOT (EXISTS ( SELECT 1
                   FROM staging.entity_retractions retraction
                  WHERE retraction.relation_id = relation.relation_id))
          ORDER BY relation.from_entity_id, relation.transaction_time DESC, relation.relation_id DESC
        ), issuer_rels AS MATERIALIZED (
         SELECT r_issues.from_entity_id AS issuer_id,
            (primary_listing.listing_id IS NOT NULL) AS is_primary,
            r_issues.transaction_time,
            r_issues.relation_id,
            pa.direct_ticker,
            pa.direct_listing
           FROM staging.entity_relations r_issues
             JOIN staging.entity_relations r_listed ON r_listed.from_entity_id = r_issues.to_entity_id AND r_listed.relation_type = 'listed_as'::text
             JOIN pivoted_aliases pa ON pa.survivor_id = r_listed.to_entity_id
             LEFT JOIN primary_listings primary_listing ON primary_listing.issuer_id = r_issues.from_entity_id AND primary_listing.listing_id = r_listed.to_entity_id
          WHERE r_issues.relation_type = 'issues'::text
        ), issuer_tickers AS (
         SELECT DISTINCT ON (issuer_rels.issuer_id) issuer_rels.issuer_id,
            issuer_rels.direct_ticker AS ticker
           FROM issuer_rels
          WHERE issuer_rels.direct_ticker IS NOT NULL
          ORDER BY issuer_rels.issuer_id, issuer_rels.is_primary DESC, issuer_rels.transaction_time DESC, issuer_rels.direct_ticker, issuer_rels.relation_id DESC
        ), instrument_tickers AS (
         SELECT DISTINCT ON (r_listed.from_entity_id) r_listed.from_entity_id AS instrument_id,
            pa.direct_ticker AS ticker
           FROM staging.entity_relations r_listed
             JOIN pivoted_aliases pa ON pa.survivor_id = r_listed.to_entity_id
          WHERE r_listed.relation_type = 'listed_as'::text AND pa.direct_ticker IS NOT NULL
          ORDER BY r_listed.from_entity_id, r_listed.transaction_time DESC, pa.direct_ticker, r_listed.relation_id DESC
        ), issuer_listings AS (
         SELECT DISTINCT ON (issuer_rels.issuer_id) issuer_rels.issuer_id,
            issuer_rels.direct_listing AS listing_id
           FROM issuer_rels
          WHERE issuer_rels.direct_listing IS NOT NULL
          ORDER BY issuer_rels.issuer_id, issuer_rels.is_primary DESC, issuer_rels.transaction_time DESC, issuer_rels.direct_listing, issuer_rels.relation_id DESC
        )
 SELECT e.entity_id,
    e.kind,
    COALESCE(pa.direct_ticker, it.ticker, inst.ticker) AS current_ticker,
    CASE
        WHEN upper(btrim(pa.name)) <> upper(btrim(COALESCE(pa.direct_ticker, it.ticker, inst.ticker, ''::text))) THEN NULLIF(btrim(pa.name), ''::text)
        ELSE NULL::text
    END AS name,
    pa.cik,
    pa.lei,
    COALESCE(pa.direct_listing, ilist.listing_id) AS listing_id,
    pa.legacy_id
   FROM staging.entities e
     JOIN survivors survivor ON survivor.entity_id = e.entity_id
     LEFT JOIN pivoted_aliases pa ON pa.survivor_id = survivor.id
     LEFT JOIN issuer_tickers it ON it.issuer_id = survivor.id
     LEFT JOIN instrument_tickers inst ON inst.instrument_id = survivor.id
     LEFT JOIN issuer_listings ilist ON ilist.issuer_id = survivor.id
WITH DATA
        $mv$;
    end if;

    if to_regclass('mart.uq_mart_entity_identity') is null then
        create unique index uq_mart_entity_identity
            on mart.entity_identity using btree (entity_id) nulls not distinct;
    end if;
    if to_regclass('mart.ix_mart_entity_identity_listing') is null then
        create index ix_mart_entity_identity_listing
            on mart.entity_identity using btree (listing_id) where (listing_id is not null);
    end if;

    -- 3. Convert mart.fund_holdings* views -> materialized views in topological order.
    if not v_holdings_is_mv then
        for v_rel in
            select unnest(array['fund_holdings_valuation', 'fund_holdings_coverage', 'fund_holdings_resolved', 'fund_holdings'])
        loop
            select c.relkind
              into v_kind
              from pg_class c
              join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'mart' and c.relname = v_rel;
            if v_kind = 'm' then
                execute format('drop materialized view mart.%I', v_rel);
            elsif v_kind = 'v' then
                execute format('drop view mart.%I', v_rel);
            end if;
        end loop;

        execute $mv$
create materialized view mart.fund_holdings as
 SELECT DISTINCT ON (facts.fund_id, facts.report_period, facts.transaction_time, (COALESCE(facts.isin, ''::text)), (COALESCE(facts.cusip, ''::text)), facts.holding_name) facts.fund_id::text AS fund_id,
    fund_name.value AS fund_name,
    facts.holding_id::text AS holding_id,
    facts.holding_name,
    facts.isin,
    facts.cusip,
    facts.report_period,
    facts.transaction_time,
    facts.percent_of_net_assets,
    facts.value_usd,
    facts.balance,
    facts.confidence,
    facts.raw_ref
   FROM staging.fund_holding_facts facts
     LEFT JOIN LATERAL ( SELECT alias.value
           FROM staging.entity_aliases alias
          WHERE alias.entity_id = facts.fund_id AND alias.scheme = 'name'::text
          ORDER BY alias.confidence DESC, alias.transaction_time DESC, alias.alias_id DESC
         LIMIT 1) fund_name ON true
  ORDER BY facts.fund_id, facts.report_period, facts.transaction_time, (COALESCE(facts.isin, ''::text)), (COALESCE(facts.cusip, ''::text)), facts.holding_name, facts.recorded_at DESC, facts.id DESC
WITH DATA
        $mv$;

        execute $mv$
create materialized view mart.fund_holdings_resolved as
 WITH RECURSIVE merge_edges AS (
         SELECT DISTINCT ON (relation.from_entity_id) relation.from_entity_id,
            relation.to_entity_id
           FROM staging.entity_relations relation
          WHERE relation.relation_type = 'superseded_by'::text
            AND NOT (EXISTS ( SELECT 1
                   FROM staging.entity_retractions retraction
                  WHERE retraction.relation_id = relation.relation_id))
          ORDER BY relation.from_entity_id, relation.transaction_time DESC, relation.relation_id DESC
        ), survivor_walk(entity_id, current_id, depth) AS (
         SELECT entity.entity_id,
            entity.entity_id,
            0 AS depth
           FROM staging.entities entity
        UNION ALL
         SELECT walk.entity_id,
            edge.to_entity_id,
            walk.depth + 1
           FROM survivor_walk walk
             JOIN merge_edges edge ON edge.from_entity_id = walk.current_id
          WHERE walk.depth < 32
        ), survivors AS (
         SELECT DISTINCT ON (walk.entity_id) walk.entity_id,
            walk.current_id AS survivor_id
           FROM survivor_walk walk
          ORDER BY walk.entity_id, walk.depth DESC
        ), resolved AS (
         SELECT holdings.fund_id,
            holdings.fund_name,
            holdings.holding_id,
            holdings.holding_name,
            holdings.isin,
            holdings.cusip,
            holdings.report_period,
            holdings.transaction_time,
            holdings.percent_of_net_assets,
            holdings.value_usd,
            holdings.balance,
            holdings.confidence,
            holdings.raw_ref,
            COALESCE(
                NULLIF(holdings.holding_id, ''::text)::uuid,
                ( SELECT alias.entity_id
                       FROM staging.entity_aliases alias
                      WHERE alias.scheme = 'isin'::text AND alias.value = upper(holdings.isin)
                      ORDER BY alias.transaction_time DESC, alias.confidence DESC, alias.alias_id DESC
                     LIMIT 1)
            ) AS holding_uuid
           FROM mart.fund_holdings holdings
        ), linked AS (
         SELECT resolved.*,
            COALESCE(
                ( SELECT COALESCE(sup.survivor_id, rel.from_entity_id)
                       FROM staging.entity_relations rel
                         LEFT JOIN survivors sup ON sup.entity_id = rel.from_entity_id
                      WHERE rel.to_entity_id = resolved.holding_uuid AND rel.relation_type = 'issues'::text
                      ORDER BY rel.transaction_time DESC, rel.relation_id DESC
                     LIMIT 1),
                COALESCE(self_sup.survivor_id, resolved.holding_uuid)
            ) AS issuer_uuid
           FROM resolved
             LEFT JOIN survivors self_sup ON self_sup.entity_id = resolved.holding_uuid
        )
 SELECT linked.fund_id,
    linked.fund_name,
    linked.report_period,
    linked.transaction_time,
    linked.holding_name,
    linked.isin,
    linked.cusip,
    linked.percent_of_net_assets,
    linked.value_usd,
    linked.issuer_uuid::text AS issuer_entity,
    COALESCE(listing_alias.ticker, holding_ticker.value, issuer_ticker.value) AS ticker,
    COALESCE(
        listing_alias.listing_id,
        CASE
            WHEN COALESCE(listing_alias.ticker, holding_ticker.value, issuer_ticker.value) IS NOT NULL THEN 'listing:xnas:'::text || lower(COALESCE(listing_alias.ticker, holding_ticker.value, issuer_ticker.value))
            ELSE NULL::text
        END
    ) AS listing_id
   FROM linked
     LEFT JOIN LATERAL ( SELECT
                CASE
                    WHEN alias.scheme = 'mic-ticker'::text THEN upper(split_part(alias.value, ':'::text, 2))
                    WHEN alias.scheme = 'legacy-id'::text AND alias.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text THEN upper(split_part(alias.value, ':'::text, 3))
                    WHEN alias.scheme = 'ticker'::text THEN upper(alias.value)
                    ELSE NULL::text
                END AS ticker,
                CASE
                    WHEN alias.scheme = 'legacy-id'::text AND alias.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text THEN alias.value
                    WHEN alias.scheme = 'mic-ticker'::text THEN ('listing:'::text || lower(split_part(alias.value, ':'::text, 1))) || ':'::text || lower(split_part(alias.value, ':'::text, 2))
                    ELSE NULL::text
                END AS listing_id
           FROM staging.entity_relations rel
             JOIN staging.entity_aliases alias ON alias.entity_id = rel.to_entity_id
          WHERE rel.from_entity_id = linked.holding_uuid
            AND rel.relation_type = 'listed_as'::text
            AND (alias.scheme = 'mic-ticker'::text OR alias.scheme = 'ticker'::text OR (alias.scheme = 'legacy-id'::text AND alias.value ~ '^listing:[0-9a-z]{4}:[0-9a-z][0-9a-z.\-]*$'::text))
          ORDER BY (alias.scheme = 'mic-ticker'::text) DESC, (alias.scheme = 'legacy-id'::text) DESC, alias.confidence DESC, alias.transaction_time DESC, alias.alias_id DESC
         LIMIT 1) listing_alias ON true
     LEFT JOIN LATERAL ( SELECT upper(alias.value) AS value
           FROM staging.entity_aliases alias
          WHERE alias.entity_id = linked.holding_uuid AND alias.scheme = 'ticker'::text
          ORDER BY alias.transaction_time DESC, alias.confidence DESC, alias.alias_id DESC
         LIMIT 1) holding_ticker ON true
     LEFT JOIN LATERAL ( SELECT upper(alias.value) AS value
           FROM staging.entity_aliases alias
          WHERE alias.entity_id = linked.issuer_uuid AND alias.scheme = 'ticker'::text
          ORDER BY alias.transaction_time DESC, alias.confidence DESC, alias.alias_id DESC
         LIMIT 1) issuer_ticker ON true
WITH DATA
        $mv$;

        execute $mv$
create materialized view mart.fund_holdings_coverage as
 SELECT fund_holdings_resolved.fund_id,
    fund_holdings_resolved.report_period,
    fund_holdings_resolved.transaction_time,
    count(*) AS lines,
    count(*) FILTER (WHERE fund_holdings_resolved.listing_id IS NOT NULL) AS resolved_lines,
    COALESCE(sum(fund_holdings_resolved.percent_of_net_assets), 0::numeric) AS total_weight_pct,
    COALESCE(sum(fund_holdings_resolved.percent_of_net_assets) FILTER (WHERE fund_holdings_resolved.listing_id IS NOT NULL), 0::numeric) AS resolved_weight_pct
   FROM mart.fund_holdings_resolved
  GROUP BY fund_holdings_resolved.fund_id, fund_holdings_resolved.report_period, fund_holdings_resolved.transaction_time
WITH DATA
        $mv$;

        execute $mv$
create materialized view mart.fund_holdings_valuation as
 WITH newest AS (
         SELECT DISTINCT ON (fund_holdings.fund_id) fund_holdings.fund_id,
            fund_holdings.report_period,
            fund_holdings.transaction_time
           FROM mart.fund_holdings
          ORDER BY fund_holdings.fund_id, fund_holdings.report_period DESC, fund_holdings.transaction_time DESC
        )
 SELECT resolved.fund_id,
    resolved.fund_name,
    resolved.report_period,
    resolved.transaction_time,
    resolved.holding_name,
    resolved.isin,
    resolved.cusip,
    resolved.percent_of_net_assets,
    resolved.value_usd,
    resolved.issuer_entity,
    resolved.ticker,
    resolved.listing_id
   FROM mart.fund_holdings_resolved resolved
     JOIN newest USING (fund_id, report_period, transaction_time)
WITH DATA
        $mv$;
    end if;

    if to_regclass('mart.uq_mart_fund_holdings') is null then
        create unique index uq_mart_fund_holdings
            on mart.fund_holdings using btree (fund_id, report_period, transaction_time, isin, cusip, holding_name) nulls not distinct;
    end if;
    if to_regclass('mart.uq_mart_fund_holdings_resolved') is null then
        create unique index uq_mart_fund_holdings_resolved
            on mart.fund_holdings_resolved using btree (fund_id, report_period, transaction_time, isin, cusip, holding_name) nulls not distinct;
    end if;
    if to_regclass('mart.uq_mart_fund_holdings_coverage') is null then
        create unique index uq_mart_fund_holdings_coverage
            on mart.fund_holdings_coverage using btree (fund_id, report_period, transaction_time) nulls not distinct;
    end if;
    if to_regclass('mart.uq_mart_fund_holdings_valuation') is null then
        create unique index uq_mart_fund_holdings_valuation
            on mart.fund_holdings_valuation using btree (fund_id, report_period, transaction_time, isin, cusip, holding_name) nulls not distinct;
    end if;

    if exists (select 1 from pg_roles where rolname = 'mart_readonly') then
        grant select on
            mart.entity_identity,
            mart.fund_holdings,
            mart.fund_holdings_resolved,
            mart.fund_holdings_coverage,
            mart.fund_holdings_valuation
        to mart_readonly;
    end if;
    if exists (select 1 from pg_roles where rolname = 'app_ops_reader') then
        grant select on
            mart.fund_holdings,
            mart.fund_holdings_resolved,
            mart.fund_holdings_coverage,
            mart.fund_holdings_valuation
        to app_ops_reader;
    end if;
end
$$;
