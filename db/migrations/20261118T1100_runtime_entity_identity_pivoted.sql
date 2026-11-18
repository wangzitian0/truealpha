-- Replace mart.entity_identity multi-CTE scheme scans with one GROUP BY materialized pivot (#1716).
do $$
begin
    if to_regclass('mart.entity_identity') is null
       or pg_get_viewdef('mart.entity_identity'::regclass) !~ 'pivoted_aliases'
    then
        execute $vc$create or replace view mart.entity_identity as
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
             LEFT JOIN issuer_listings ilist ON ilist.issuer_id = survivor.id$vc$;
    end if;
end
$$;
