-- #877 PR-2: one opaque identity per real-world entity; every id we hold is a typed alias.
--
-- Owner decision 2026-09-17: LEI, CIK, CUSIP, FIGI, ISIN, ticker@MIC, moomoo codes and our
-- own `issuer:lei:…` / `security:figi:…` strings are identifiers of different schemes, not
-- competing canonical ids. An entity is named by a UUID that means nothing; everything else
-- is an alias with a scheme, a value, a validity interval, a knowable-at time, provenance and
-- a confidence. The design is docs/entity-identity.md.
--
-- This migration adds the store. The next one (…T0430_datahub_entity_backfill) fills it from
-- what is already stored. Neither switches a reader or a capture: payloads keep their legacy
-- ids until PR-3.
--
--   staging.entity_kinds            the ontology's type hierarchy (issuer is-an organization …)
--   staging.entity_alias_schemes    which identifier schemes exist, what they identify, their
--                                   canonical form and whether a value may change hands
--   staging.entities                one row per minted UUID
--   staging.entity_aliases          scheme + value -> entity, valid [valid_from, valid_to)
--   staging.entity_relation_types   typed edges with domain and range kinds
--   staging.entity_relations        issuer-issues-instrument, merges (superseded_by), …
--   staging.entity_retractions      the only way a claim ends: a later row, never an UPDATE
--   staging.entity_mint()           the one way an entity is created
--   staging.entity_resolve()        alias -> surviving entity at a valid date, as known at a time
--
-- Ids are derived, never random (the repository's identity rule: a run id comes from its
-- cutoff and universe, an observation id from its bytes). An entity id is the UUIDv5 of its
-- birth alias, `scheme:canonical value`, under its kind's namespace; a relation id is the
-- UUIDv5 of its claim under its type's namespace. The same evidence mints the same ids in
-- every database. After minting the id is opaque: a birth alias later found wrong is
-- retracted, and the id stays.
--
-- Every table is append-only. A merge adds `same_as` and `superseded_by` edges and rewrites
-- nothing; a wrong alias is ended by a retraction row; the resolver follows the edges.

-- ---------------------------------------------------------------------------------------
-- Guards and minting
-- ---------------------------------------------------------------------------------------

create or replace function staging.reject_entity_mutation()
returns trigger language plpgsql as $$
begin
    raise exception 'entity identity records are append-only; add an alias, relation or retraction instead';
end;
$$;

-- RFC 9562 UUIDv5: the first 16 bytes of SHA-1(namespace || name), with the version nibble
-- set to 5 and the variant bits to 10. Equal to Python's uuid.uuid5(namespace, name).
-- pgcrypto's digest() comes from 0023; the search_path pin is 0044's lesson.
create or replace function staging.entity_uuid_v5(p_namespace uuid, p_name text)
returns uuid
language plpgsql immutable strict parallel safe
set search_path = pg_catalog, public
as $$
declare
    v_bytes bytea := substring(digest(uuid_send(p_namespace) || convert_to(p_name, 'UTF8'), 'sha1') from 1 for 16);
begin
    v_bytes := set_byte(v_bytes, 6, (get_byte(v_bytes, 6) & 15) | 80);
    v_bytes := set_byte(v_bytes, 8, (get_byte(v_bytes, 8) & 63) | 128);
    return encode(v_bytes, 'hex')::uuid;
end;
$$;

-- The namespace of one kind or relation type: UUIDv5 of `kind:<kind>` or
-- `relation:<type>` under the root, and the root is the RFC 9562 URL namespace applied to
-- this design's URL. Stored on the registry rows so readers never recompute it.
create or replace function staging.entity_uuid_namespace(p_label text)
returns uuid
language sql immutable strict parallel safe
as $$
    select staging.entity_uuid_v5(
        staging.entity_uuid_v5(
            '6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid,
            'https://github.com/wangzitian0/truealpha/blob/main/docs/entity-identity.md'),
        p_label);
$$;

-- Canonical text for the parts of a derived name that have a session-dependent rendering.
create or replace function staging.entity_name_date(p_date date)
returns text
language sql immutable strict parallel safe
as $$
    select case when isfinite(p_date) then to_char(p_date, 'YYYY-MM-DD') else p_date::text end;
$$;

create or replace function staging.entity_name_time(p_time timestamptz)
returns text
language sql immutable strict parallel safe
as $$
    select to_char(p_time at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
$$;

-- ---------------------------------------------------------------------------------------
-- Ontology: kinds
-- ---------------------------------------------------------------------------------------

create table if not exists staging.entity_kinds (
    kind            text primary key check (kind ~ '^[a-z][a-z_]*$'),
    parent_kind     text references staging.entity_kinds (kind),
    is_abstract     boolean not null,
    -- The UUIDv5 namespace entity ids of this kind are minted under; none for abstract kinds.
    uuid_namespace  uuid unique,
    description     text not null check (length(description) > 0),
    recorded_at     timestamptz not null default clock_timestamp(),
    constraint entity_kind_single_root check ((parent_kind is null) = (kind = 'entity')),
    constraint entity_kind_namespace check (
        (is_abstract and uuid_namespace is null)
        or (not is_abstract and uuid_namespace = staging.entity_uuid_namespace('kind:' || kind)))
);

drop trigger if exists reject_mutation on staging.entity_kinds;
create trigger reject_mutation
before update or delete on staging.entity_kinds
for each row execute function staging.reject_entity_mutation();

insert into staging.entity_kinds (kind, parent_kind, is_abstract, uuid_namespace, description)
select kind, parent_kind, is_abstract,
       case when not is_abstract then staging.entity_uuid_namespace('kind:' || kind) end,
       description
from (values
    ('entity', null, true, 'Root of the ontology. Nothing is minted as a bare entity.'),
    ('agent', 'entity', true, 'Something that acts: organizations and people.'),
    ('organization', 'agent', true, 'A legal or operating organization.'),
    ('issuer', 'organization', false,
     'An organization that issues financial instruments (an operating company).'),
    ('fund', 'issuer', false,
     'A registered fund series (for example an ETF): issues its own shares and holds instruments.'),
    ('person', 'agent', false, 'A natural person (analyst, executive). No writer yet.'),
    ('instrument', 'entity', false,
     'A financial instrument at share-class level (what CUSIP, ISIN and a share-class FIGI name).'),
    ('listing', 'entity', false,
     'One instrument''s trading line on one venue (what MIC plus symbol names).'),
    ('universe', 'entity', false, 'A governed membership list (TOPT, QQQ, canary). No writer yet.'),
    ('segment', 'entity', false, 'A reportable business segment of an issuer. No writer yet.'),
    ('product', 'entity', false, 'A product or service line. No writer yet.')
) as seed (kind, parent_kind, is_abstract, description)
on conflict (kind) do nothing;

-- Is `p_kind` the kind `p_ancestor` or a descendant of it? plpgsql rather than a recursive
-- SQL function: every alias and relation insert asks, and plpgsql keeps its plan for the
-- session where a SQL function is planned again for each calling statement.
create or replace function staging.entity_kind_is_a(p_kind text, p_ancestor text)
returns boolean
language plpgsql stable
as $$
declare
    v_kind text := p_kind;
    v_depth integer := 0;
begin
    while v_kind is not null and v_depth <= 16 loop
        if v_kind = p_ancestor then
            return true;
        end if;
        select parent_kind into v_kind from staging.entity_kinds where kind = v_kind;
        v_depth := v_depth + 1;
    end loop;
    return false;
end;
$$;

-- ---------------------------------------------------------------------------------------
-- Alias schemes
-- ---------------------------------------------------------------------------------------

create table if not exists staging.entity_alias_schemes (
    scheme           text primary key check (scheme ~ '^[a-z][a-z0-9-]*$'),
    applies_to_kind  text not null references staging.entity_kinds (kind),
    -- At most one entity holds a value at any valid instant. A non-unique scheme (a name)
    -- labels entities and cannot resolve one.
    is_unique        boolean not null,
    -- The authority may hand a value to a different entity later (tickers, CUSIPs). A value
    -- that never changes hands is valid for all time once assigned.
    value_reusable   boolean not null,
    -- Values are stored in canonical form (staging.entity_alias_normalize) and must match.
    value_pattern    text not null,
    authority        text not null check (length(authority) > 0),
    description      text not null check (length(description) > 0),
    recorded_at      timestamptz not null default clock_timestamp()
);

drop trigger if exists reject_mutation on staging.entity_alias_schemes;
create trigger reject_mutation
before update or delete on staging.entity_alias_schemes
for each row execute function staging.reject_entity_mutation();

insert into staging.entity_alias_schemes
    (scheme, applies_to_kind, is_unique, value_reusable, value_pattern, authority, description) values
    ('legacy-id', 'entity', true, false, '^[a-z][a-z0-9-]*:[a-z0-9-]+:\S+$', 'TrueAlpha',
     'A pre-#877 text id (issuer:lei:…, security:figi:…, listing:xnas:aapl). Case-sensitive, stored verbatim.'),
    ('lei', 'organization', true, false, '^[0-9A-Z]{18}[0-9]{2}$', 'GLEIF (ISO 17442)',
     'Legal Entity Identifier.'),
    ('cik', 'organization', true, false, '^[0-9]{10}$', 'SEC EDGAR',
     'Central Index Key, zero-padded to ten digits.'),
    ('cusip', 'instrument', true, true, '^[0-9A-Z*@#]{8}[0-9]$', 'CUSIP Global Services',
     'Nine-character CUSIP as printed in SEC filings. A retired number can be reassigned.'),
    ('isin', 'instrument', true, true, '^[A-Z]{2}[0-9A-Z]{9}[0-9]$', 'ISO 6166',
     'International Securities Identification Number. US and CA ISINs embed the CUSIP, so they inherit its reuse.'),
    ('figi', 'instrument', true, false, '^[B-DF-HJ-NP-TV-Z]{2}G[B-DF-HJ-NP-TV-Z0-9]{8}[0-9]$', 'OpenFIGI (OMG FIGI)',
     'Share-class FIGI where the vendor returns one. FIGI values are never reused at any level.'),
    ('mic-ticker', 'listing', true, true, '^[A-Z0-9]{4}:[A-Z0-9][A-Z0-9.\-]*$', 'ISO 10383 MIC + venue symbol',
     'Venue MIC and trading symbol, for example XNAS:AAPL. Symbols are reassigned over time.'),
    ('sec-series', 'fund', true, false, '^S[0-9]{9}$', 'SEC EDGAR',
     'Registered fund series id (S000…). No writer yet.'),
    ('moomoo-code', 'listing', true, true, '^[A-Z]{2}\.[A-Z0-9][A-Z0-9.\-]*$', 'moomoo OpenAPI',
     'Market-prefixed quote code, for example US.AAPL. No writer yet.'),
    ('name', 'entity', false, true, '^\S(.*\S)?$', 'the asserting source',
     'A display label. Not unique; never resolves an entity.')
on conflict (scheme) do nothing;

-- Canonical form of a value in a scheme. Identity for anything that is not a recognized
-- shape, so a malformed value stays malformed and the pattern check refuses it.
create or replace function staging.entity_alias_normalize(p_scheme text, p_value text)
returns text
language sql immutable
as $$
    select case
        when p_value is null then null
        when p_scheme in ('legacy-id', 'name') then btrim(p_value)
        when p_scheme = 'cik' and btrim(p_value) ~ '^[0-9]{1,10}$' then lpad(btrim(p_value), 10, '0')
        else upper(btrim(p_value))
    end;
$$;

-- ---------------------------------------------------------------------------------------
-- Entities
-- ---------------------------------------------------------------------------------------

create table if not exists staging.entities (
    entity_id         uuid primary key,
    kind              text not null references staging.entity_kinds (kind),
    mint_rule         text not null check (mint_rule in ('uuidv5:v1')),
    minted_at         timestamptz not null default clock_timestamp(),
    minted_by         text not null check (length(minted_by) > 0),
    -- The alias whose resolution miss minted this entity; the id is derived from it. After
    -- minting the id is opaque: if this alias is retracted the entity keeps its id.
    birth_scheme      text not null references staging.entity_alias_schemes (scheme),
    birth_value       text not null,
    -- 1 unless an entity already took the id this birth alias derives (its alias was
    -- retracted and the value named something new); then the name gets a `#n` suffix.
    birth_generation  integer not null default 1 check (birth_generation >= 1),
    constraint entity_id_is_uuidv5 check (substring(entity_id::text from 15 for 1) = '5')
);

drop trigger if exists reject_mutation on staging.entities;
create trigger reject_mutation
before update or delete on staging.entities
for each row execute function staging.reject_entity_mutation();

-- The name an entity id is derived from.
create or replace function staging.entity_birth_name(p_scheme text, p_value text, p_generation integer)
returns text
language sql immutable strict parallel safe
as $$
    select p_scheme || ':' || p_value || case when p_generation > 1 then '#' || p_generation else '' end;
$$;

create or replace function staging.validate_entity()
returns trigger language plpgsql as $$
declare
    v_kind staging.entity_kinds%rowtype;
begin
    select * into v_kind from staging.entity_kinds where kind = new.kind;
    if v_kind.is_abstract then
        raise exception 'entity kind % is abstract; mint a concrete kind', new.kind;
    end if;
    if new.birth_value is distinct from staging.entity_alias_normalize(new.birth_scheme, new.birth_value) then
        raise exception 'birth alias %:% is not in canonical form', new.birth_scheme, new.birth_value;
    end if;
    if new.entity_id <> staging.entity_uuid_v5(
            v_kind.uuid_namespace,
            staging.entity_birth_name(new.birth_scheme, new.birth_value, new.birth_generation)) then
        raise exception 'entity id % is not derived from its birth alias %:% (generation %)',
            new.entity_id, new.birth_scheme, new.birth_value, new.birth_generation;
    end if;
    return new;
end;
$$;

drop trigger if exists validate_entity on staging.entities;
create trigger validate_entity
before insert on staging.entities
for each row execute function staging.validate_entity();

-- ---------------------------------------------------------------------------------------
-- Aliases
-- ---------------------------------------------------------------------------------------

create table if not exists staging.entity_aliases (
    alias_id          bigint generated always as identity primary key,
    entity_id         uuid not null references staging.entities (entity_id),
    scheme            text not null references staging.entity_alias_schemes (scheme),
    value             text not null,
    -- Real-world validity, [valid_from, valid_to). '-infinity' = since the value exists.
    valid_from        date not null,
    valid_to          date,
    -- When the claim became knowable, taken from its evidence. No default (init.md §6).
    transaction_time  timestamptz not null,
    recorded_at       timestamptz not null default clock_timestamp(),
    source            text not null check (length(source) > 0),
    raw_ref           text not null check (length(raw_ref) > 0),
    -- asserted: a source named this entity by this value; parsed: the value is spelled out
    -- inside another alias of the same entity; crosswalk: stored evidence joins two schemes.
    method            text not null check (method in ('asserted', 'parsed', 'crosswalk')),
    confidence        numeric not null check (confidence >= 0 and confidence <= 1),
    evidence          jsonb not null default '{}'::jsonb check (jsonb_typeof(evidence) = 'object'),
    mapping_version   text not null check (length(mapping_version) > 0),
    constraint entity_alias_period check (valid_to is null or valid_to > valid_from),
    constraint entity_alias_recorded_after_knowable check (recorded_at >= transaction_time),
    constraint entity_alias_vintage unique (scheme, value, entity_id, valid_from, transaction_time, method, source)
);

create index if not exists idx_entity_aliases_lookup on staging.entity_aliases (scheme, value);
create index if not exists idx_entity_aliases_entity on staging.entity_aliases (entity_id);

drop trigger if exists reject_mutation on staging.entity_aliases;
create trigger reject_mutation
before update or delete on staging.entity_aliases
for each row execute function staging.reject_entity_mutation();

-- ---------------------------------------------------------------------------------------
-- Relations
-- ---------------------------------------------------------------------------------------

create table if not exists staging.entity_relation_types (
    relation_type  text primary key check (relation_type ~ '^[a-z][a-z_]*$'),
    domain_kind    text not null references staging.entity_kinds (kind),
    range_kind     text not null references staging.entity_kinds (kind),
    -- The UUIDv5 namespace relation ids of this type are derived under.
    uuid_namespace uuid not null unique,
    -- Both ends must be of one kind (the identity edges).
    same_kind      boolean not null,
    -- Identity bookkeeping (merge, split) rather than a fact about the world.
    is_identity    boolean not null,
    inverse_name   text not null check (inverse_name ~ '^[a-z][a-z_]*$'),
    description    text not null check (length(description) > 0),
    recorded_at    timestamptz not null default clock_timestamp(),
    constraint entity_relation_type_namespace
        check (uuid_namespace = staging.entity_uuid_namespace('relation:' || relation_type))
);

drop trigger if exists reject_mutation on staging.entity_relation_types;
create trigger reject_mutation
before update or delete on staging.entity_relation_types
for each row execute function staging.reject_entity_mutation();

insert into staging.entity_relation_types
    (relation_type, domain_kind, range_kind, same_kind, is_identity, inverse_name, description, uuid_namespace)
select seed.*, staging.entity_uuid_namespace('relation:' || seed.relation_type)
from (values
    ('issues', 'issuer', 'instrument', false, false, 'issued_by',
     'The issuer of an instrument.'),
    ('listed_as', 'instrument', 'listing', false, false, 'listing_of',
     'An instrument trades as this listing. A listing belongs to one instrument at a time.'),
    ('holds', 'fund', 'instrument', false, false, 'held_by',
     'A fund holds an instrument. Weights stay in staging.fund_holding_facts. No writer yet.'),
    ('member_of', 'entity', 'universe', false, false, 'has_member',
     'Membership of a governed universe over a validity interval. No writer yet.'),
    ('subsidiary_of', 'organization', 'organization', false, false, 'parent_of',
     'Corporate control. No writer yet.'),
    ('successor_of', 'organization', 'organization', false, false, 'predecessor_of',
     'A reorganized successor of an organization (the #496 holdco case). No writer yet.'),
    ('segment_of', 'segment', 'issuer', false, false, 'has_segment',
     'A reportable segment of an issuer. No writer yet.'),
    ('produces', 'organization', 'product', false, false, 'produced_by',
     'An organization offers a product. No writer yet.'),
    ('supplies_to', 'organization', 'organization', false, false, 'customer_of',
     'A disclosed supplier relationship. No writer yet.'),
    ('covers', 'person', 'issuer', false, false, 'covered_by',
     'An analyst covers an issuer. No writer yet.'),
    ('same_as', 'entity', 'entity', true, true, 'same_as',
     'Evidence that two entities are one real-world thing.'),
    ('superseded_by', 'entity', 'entity', true, true, 'supersedes',
     'A merge: the source entity''s identity continues as the target. The resolver follows it.'),
    ('split_into', 'entity', 'entity', true, true, 'split_from',
     'A split: from valid_from on, part of the source entity continues as the target.')
) as seed (relation_type, domain_kind, range_kind, same_kind, is_identity, inverse_name, description)
on conflict (relation_type) do nothing;

create table if not exists staging.entity_relations (
    relation_id       uuid primary key,
    relation_type     text not null references staging.entity_relation_types (relation_type),
    from_entity_id    uuid not null references staging.entities (entity_id),
    to_entity_id      uuid not null references staging.entities (entity_id),
    valid_from        date not null,
    valid_to          date,
    transaction_time  timestamptz not null,
    recorded_at       timestamptz not null default clock_timestamp(),
    source            text not null check (length(source) > 0),
    raw_ref           text not null check (length(raw_ref) > 0),
    method            text not null check (method in ('asserted', 'derived', 'crosswalk', 'merge', 'split')),
    confidence        numeric not null check (confidence >= 0 and confidence <= 1),
    evidence          jsonb not null default '{}'::jsonb check (jsonb_typeof(evidence) = 'object'),
    attributes        jsonb not null default '{}'::jsonb check (jsonb_typeof(attributes) = 'object'),
    mapping_version   text not null check (length(mapping_version) > 0),
    constraint entity_relation_not_self check (from_entity_id <> to_entity_id),
    constraint entity_relation_period check (valid_to is null or valid_to > valid_from),
    constraint entity_relation_recorded_after_knowable check (recorded_at >= transaction_time),
    constraint entity_relation_id_is_uuidv5 check (substring(relation_id::text from 15 for 1) = '5')
);

create index if not exists idx_entity_relations_from on staging.entity_relations (from_entity_id, relation_type);
create index if not exists idx_entity_relations_to on staging.entity_relations (to_entity_id, relation_type);

drop trigger if exists reject_mutation on staging.entity_relations;
create trigger reject_mutation
before update or delete on staging.entity_relations
for each row execute function staging.reject_entity_mutation();

-- ---------------------------------------------------------------------------------------
-- Retractions
-- ---------------------------------------------------------------------------------------

create table if not exists staging.entity_retractions (
    retraction_id     bigint generated always as identity primary key,
    alias_id          bigint references staging.entity_aliases (alias_id),
    relation_id       uuid references staging.entity_relations (relation_id),
    -- The claim holds only before this date. A date at or before the claim's valid_from
    -- withdraws it entirely. Identity edges (merge, split) are withdrawn by any retraction.
    valid_to          date not null,
    reason            text not null check (length(reason) > 0),
    transaction_time  timestamptz not null,
    recorded_at       timestamptz not null default clock_timestamp(),
    source            text not null check (length(source) > 0),
    raw_ref           text not null check (length(raw_ref) > 0),
    constraint entity_retraction_one_target check (num_nonnulls(alias_id, relation_id) = 1),
    constraint entity_retraction_recorded_after_knowable check (recorded_at >= transaction_time)
);

create index if not exists idx_entity_retractions_alias on staging.entity_retractions (alias_id) where alias_id is not null;
create index if not exists idx_entity_retractions_relation on staging.entity_retractions (relation_id) where relation_id is not null;

drop trigger if exists reject_mutation on staging.entity_retractions;
create trigger reject_mutation
before update or delete on staging.entity_retractions
for each row execute function staging.reject_entity_mutation();

-- ---------------------------------------------------------------------------------------
-- Derived ids
-- ---------------------------------------------------------------------------------------

-- A relation's id: UUIDv5 of the claim (its ends, validity start, knowable-at, source and
-- method) under the relation type's namespace. A claim asserted again from new evidence has
-- a later knowable-at and so a new id; the same claim twice is the same row.
create or replace function staging.entity_relation_uuid(
    p_relation_type text, p_from uuid, p_to uuid, p_valid_from date,
    p_transaction_time timestamptz, p_source text, p_method text)
returns uuid
language sql stable
as $$
    select staging.entity_uuid_v5(
        relation_type.uuid_namespace,
        concat_ws('|', p_from, p_to, staging.entity_name_date(p_valid_from),
                  staging.entity_name_time(p_transaction_time), p_source, p_method))
    from staging.entity_relation_types relation_type
    where relation_type.relation_type = p_relation_type;
$$;

-- The one way an entity is created: the id is derived from the birth alias under the kind's
-- namespace. The caller writes the birth alias in the same transaction (a deferred trigger
-- insists). If an entity already holds the derived id, the birth alias no longer names it
-- (the caller resolved the alias and found nothing), so the next generation is used; the
-- sequence of generations is itself determined by the store's history.
create or replace function staging.entity_mint(p_kind text, p_scheme text, p_value text, p_minted_by text)
returns uuid
language plpgsql
as $$
declare
    v_namespace uuid;
    v_value text := staging.entity_alias_normalize(p_scheme, p_value);
    v_generation integer := 1;
    v_id uuid;
begin
    select uuid_namespace into v_namespace from staging.entity_kinds where kind = p_kind;
    if v_namespace is null then
        raise exception 'entity kind % cannot be minted', p_kind;
    end if;
    perform pg_advisory_xact_lock(hashtextextended('staging.entity_mint:' || p_kind || ':' || p_scheme || ':' || v_value, 0));
    loop
        v_id := staging.entity_uuid_v5(v_namespace, staging.entity_birth_name(p_scheme, v_value, v_generation));
        exit when not exists (select 1 from staging.entities where entity_id = v_id);
        v_generation := v_generation + 1;
    end loop;
    insert into staging.entities (entity_id, kind, mint_rule, minted_by, birth_scheme, birth_value, birth_generation)
    values (v_id, p_kind, 'uuidv5:v1', p_minted_by, p_scheme, v_value, v_generation);
    return v_id;
end;
$$;

-- ---------------------------------------------------------------------------------------
-- Point-in-time reads
-- ---------------------------------------------------------------------------------------

-- The end of an alias's validity as known at `p_known_at` (null = open).
create or replace function staging.entity_alias_valid_to(p_alias_id bigint, p_known_at timestamptz)
returns date
language sql stable
as $$
    select least(alias.valid_to, min(retraction.valid_to))
    from staging.entity_aliases alias
    left join staging.entity_retractions retraction
      on retraction.alias_id = alias.alias_id
     and retraction.transaction_time <= p_known_at
    where alias.alias_id = p_alias_id
    group by alias.valid_to;
$$;

create or replace function staging.entity_relation_valid_to(p_relation_id uuid, p_known_at timestamptz)
returns date
language sql stable
as $$
    select least(relation.valid_to, min(retraction.valid_to))
    from staging.entity_relations relation
    left join staging.entity_retractions retraction
      on retraction.relation_id = relation.relation_id
     and retraction.transaction_time <= p_known_at
    where relation.relation_id = p_relation_id
    group by relation.valid_to;
$$;

-- Where an entity's identity continues, as known at `p_known_at`: follow effective
-- `superseded_by` edges to the end. Merges carry no valid-time: two ids found to name one
-- thing always did. The insert guard keeps the chain acyclic; the depth cap is a backstop.
create or replace function staging.entity_survivor(p_entity_id uuid, p_known_at timestamptz)
returns uuid
language sql stable
as $$
    with recursive chain(entity_id, depth) as (
        select p_entity_id, 0
        union all
        select edge.to_entity_id, chain.depth + 1
        from chain
        cross join lateral (
            select relation.to_entity_id
            from staging.entity_relations relation
            where relation.relation_type = 'superseded_by'
              and relation.from_entity_id = chain.entity_id
              and relation.transaction_time <= p_known_at
              and not exists (
                  select 1 from staging.entity_retractions retraction
                  where retraction.relation_id = relation.relation_id
                    and retraction.transaction_time <= p_known_at)
            order by relation.transaction_time desc, relation.relation_id desc
            limit 1
        ) edge
        where chain.depth < 32
    )
    select entity_id from chain order by depth desc limit 1;
$$;

-- The resolver: which entity did `p_scheme:p_value` name on `p_valid_at`, as known at
-- `p_known_at`? Both times are required: a default of "now" is look-ahead in a replay.
-- Returns the surviving entity, null when nothing matched, and raises when two different
-- entities match (the insert guard prevents that; a raise is louder than a guess).
create or replace function staging.entity_resolve(
    p_scheme text, p_value text, p_valid_at date, p_known_at timestamptz)
returns uuid
language plpgsql stable
as $$
declare
    v_unique boolean;
    v_value text := staging.entity_alias_normalize(p_scheme, p_value);
    v_hits uuid[];
begin
    if p_valid_at is null or p_known_at is null then
        raise exception 'entity_resolve needs an explicit valid_at and known_at';
    end if;
    select is_unique into v_unique from staging.entity_alias_schemes where scheme = p_scheme;
    if v_unique is null then
        raise exception 'unknown alias scheme %', p_scheme;
    end if;
    if not v_unique then
        raise exception 'alias scheme % labels entities and cannot resolve one', p_scheme;
    end if;
    select array_agg(distinct staging.entity_survivor(alias.entity_id, p_known_at))
      into v_hits
    from staging.entity_aliases alias
    where alias.scheme = p_scheme
      and alias.value = v_value
      and alias.transaction_time <= p_known_at
      and alias.valid_from <= p_valid_at
      and coalesce(p_valid_at < staging.entity_alias_valid_to(alias.alias_id, p_known_at), true);
    if v_hits is null then
        return null;
    end if;
    if cardinality(v_hits) > 1 then
        raise exception 'alias %:% names % entities on % as known at %',
            p_scheme, v_value, cardinality(v_hits), p_valid_at, p_known_at;
    end if;
    return v_hits[1];
end;
$$;

-- Insert guard for aliases: canonical value, a kind the scheme identifies, and for a unique
-- scheme no other entity holding the value over an overlapping validity. Both claims are
-- compared as known when the later of the two became knowable: retractions only shrink
-- validity, so no overlap then means no overlap at any later time, and before then at most
-- one of the two is visible.
create or replace function staging.validate_entity_alias()
returns trigger language plpgsql as $$
declare
    v_scheme staging.entity_alias_schemes%rowtype;
    v_kind text;
    v_other record;
    v_as_of timestamptz;
    v_other_to date;
begin
    select * into v_scheme from staging.entity_alias_schemes where scheme = new.scheme;
    if new.value is distinct from staging.entity_alias_normalize(new.scheme, new.value) then
        raise exception 'alias %:% is not in canonical form (%)',
            new.scheme, new.value, staging.entity_alias_normalize(new.scheme, new.value);
    end if;
    if new.value !~ v_scheme.value_pattern then
        raise exception 'alias %:% does not match %', new.scheme, new.value, v_scheme.value_pattern;
    end if;
    select kind into v_kind from staging.entities where entity_id = new.entity_id;
    if not staging.entity_kind_is_a(v_kind, v_scheme.applies_to_kind) then
        raise exception 'scheme % identifies %, not %', new.scheme, v_scheme.applies_to_kind, v_kind;
    end if;
    if not v_scheme.is_unique then
        return new;
    end if;
    perform pg_advisory_xact_lock(hashtextextended('staging.entity_aliases:' || new.scheme || ':' || new.value, 0));
    for v_other in
        select alias.alias_id, alias.entity_id, alias.valid_from, alias.transaction_time
        from staging.entity_aliases alias
        where alias.scheme = new.scheme and alias.value = new.value and alias.entity_id <> new.entity_id
    loop
        v_as_of := greatest(v_other.transaction_time, new.transaction_time);
        if staging.entity_survivor(v_other.entity_id, v_as_of)
           = staging.entity_survivor(new.entity_id, v_as_of) then
            continue;
        end if;
        v_other_to := staging.entity_alias_valid_to(v_other.alias_id, v_as_of);
        if v_other.valid_from < coalesce(new.valid_to, 'infinity'::date)
           and new.valid_from < coalesce(v_other_to, 'infinity'::date)
           and v_other.valid_from < coalesce(v_other_to, 'infinity'::date) then
            raise exception 'alias %:% already names entity % over an overlapping validity (alias %)',
                new.scheme, new.value, v_other.entity_id, v_other.alias_id;
        end if;
    end loop;
    return new;
end;
$$;

drop trigger if exists validate_entity_alias on staging.entity_aliases;
create trigger validate_entity_alias
before insert on staging.entity_aliases
for each row execute function staging.validate_entity_alias();

-- Insert guard for relations: domain and range kinds, same-kind identity edges, and for a
-- merge exactly one effective successor and no cycle.
create or replace function staging.validate_entity_relation()
returns trigger language plpgsql as $$
declare
    v_type staging.entity_relation_types%rowtype;
    v_from_kind text;
    v_to_kind text;
begin
    select * into v_type from staging.entity_relation_types where relation_type = new.relation_type;
    select kind into v_from_kind from staging.entities where entity_id = new.from_entity_id;
    select kind into v_to_kind from staging.entities where entity_id = new.to_entity_id;
    if not staging.entity_kind_is_a(v_from_kind, v_type.domain_kind) then
        raise exception '% starts at a %, not %', new.relation_type, v_type.domain_kind, v_from_kind;
    end if;
    if not staging.entity_kind_is_a(v_to_kind, v_type.range_kind) then
        raise exception '% ends at a %, not %', new.relation_type, v_type.range_kind, v_to_kind;
    end if;
    if v_type.same_kind and v_from_kind <> v_to_kind then
        raise exception '% joins entities of one kind, not % and %', new.relation_type, v_from_kind, v_to_kind;
    end if;
    if new.relation_id <> staging.entity_relation_uuid(
            new.relation_type, new.from_entity_id, new.to_entity_id, new.valid_from,
            new.transaction_time, new.source, new.method) then
        raise exception 'relation id % is not derived from its claim', new.relation_id;
    end if;
    if new.relation_type = 'superseded_by' then
        perform pg_advisory_xact_lock(hashtextextended('staging.entity_relations:superseded_by', 0));
        if staging.entity_survivor(new.from_entity_id, 'infinity') <> new.from_entity_id then
            raise exception 'entity % is already superseded; merge its survivor instead', new.from_entity_id;
        end if;
        if staging.entity_survivor(new.to_entity_id, 'infinity') = new.from_entity_id then
            raise exception 'merging % into % would close a cycle', new.from_entity_id, new.to_entity_id;
        end if;
    end if;
    return new;
end;
$$;

drop trigger if exists validate_entity_relation on staging.entity_relations;
create trigger validate_entity_relation
before insert on staging.entity_relations
for each row execute function staging.validate_entity_relation();

-- An entity no alias names is unreachable: its birth alias must exist by commit.
create or replace function staging.require_entity_birth_alias()
returns trigger language plpgsql as $$
begin
    if not exists (
        select 1 from staging.entity_aliases alias
        where alias.entity_id = new.entity_id
          and alias.scheme = new.birth_scheme
          and alias.value = new.birth_value
    ) then
        raise exception 'entity % was minted without its birth alias %:%',
            new.entity_id, new.birth_scheme, new.birth_value;
    end if;
    return null;
end;
$$;

drop trigger if exists require_birth_alias on staging.entities;
create constraint trigger require_birth_alias
after insert on staging.entities
deferrable initially deferred
for each row execute function staging.require_entity_birth_alias();

-- Current state, for set-based readers and the admin surface: every alias with its
-- effective end and the entity its holder survives as. Point-in-time reads use
-- staging.entity_resolve with explicit times.
create or replace view staging.entity_alias_resolution as
select alias.alias_id,
       alias.scheme,
       alias.value,
       alias.entity_id as holder_entity_id,
       staging.entity_survivor(alias.entity_id, 'infinity') as entity_id,
       entity.kind,
       alias.valid_from,
       staging.entity_alias_valid_to(alias.alias_id, 'infinity') as valid_to,
       alias.transaction_time,
       alias.source,
       alias.method,
       alias.confidence
from staging.entity_aliases alias
join staging.entities entity on entity.entity_id = alias.entity_id;

-- Export shape for a graph database: stable node and edge ids, labels from the kind path.
create or replace view staging.entity_graph_nodes as
select entity.entity_id,
       entity.kind,
       (with recursive lineage(kind, depth) as (
            select entity.kind, 0
            union all
            select parent.parent_kind, lineage.depth + 1
            from lineage join staging.entity_kinds parent on parent.kind = lineage.kind
            where parent.parent_kind is not null and lineage.depth < 16)
        select array_agg(kind order by depth) from lineage) as labels,
       staging.entity_survivor(entity.entity_id, 'infinity') as survivor_entity_id,
       coalesce((
           select jsonb_agg(jsonb_build_object(
                      'scheme', current.scheme, 'value', current.value,
                      'valid_from', current.valid_from, 'valid_to', current.valid_to)
                  order by current.scheme, current.value, current.valid_from)
           from staging.entity_alias_resolution current
           where current.holder_entity_id = entity.entity_id
             and (current.valid_to is null or current.valid_to > current.valid_from)
       ), '[]'::jsonb) as aliases,
       entity.minted_at
from staging.entities entity;

create or replace view staging.entity_graph_edges as
select relation.relation_id,
       relation.relation_type,
       relation_type.is_identity,
       relation.from_entity_id,
       relation.to_entity_id,
       relation.valid_from,
       staging.entity_relation_valid_to(relation.relation_id, 'infinity') as valid_to,
       relation.transaction_time,
       relation.confidence,
       relation.source,
       relation.attributes
from staging.entity_relations relation
join staging.entity_relation_types relation_type using (relation_type)
where not exists (
    select 1 from staging.entity_retractions retraction
    where retraction.relation_id = relation.relation_id
      and (relation_type.is_identity or retraction.valid_to <= relation.valid_from));
