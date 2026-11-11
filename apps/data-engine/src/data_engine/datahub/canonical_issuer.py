"""Wide-row issuer id resolution for universe members (#1079, docs/entity-identity.md §5)."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

import psycopg

from data_engine.datahub.primary_listing import choose_primary_listings
from data_engine.datahub.resolve_coordinates import is_uuid, lookup_entity, parse_alias

__all__ = (
    "DUPLICATE_CORPUS_ID",
    "HEAD_MEMBER_NOT_IN_UNIVERSE",
    "JOIN_FLOOR",
    "JOIN_FLOOR_MIN_ISSUERS",
    "MEMBER_RESOLVES_ELSEWHERE",
    "NOT_IN_WIDE_ROW",
    "NO_CANONICAL_ISSUER_ID",
    "CanonicalIssuer",
    "CanonicalUniverse",
    "ConflictingPredecessorError",
    "UnmappedIssuer",
    "UnvisitedIssuer",
    "account_for_head_members",
    "canonicalize_universe",
    "expand_issuer_alias_ids",
    "is_canonical_issuer_id",
    "reason_counts",
    "require_canonical_issuer_id",
    "signed_predecessor_ciks",
    "stored_entity_of",
)

NO_CANONICAL_ISSUER_ID = "no_canonical_issuer_id"
NOT_IN_WIDE_ROW = "not_in_wide_row"
HEAD_MEMBER_NOT_IN_UNIVERSE = "head_member_not_in_universe"
DUPLICATE_CORPUS_ID = "duplicate_corpus_id"
MEMBER_RESOLVES_ELSEWHERE = "member_resolves_elsewhere"

JOIN_FLOOR = 0.5
JOIN_FLOOR_MIN_ISSUERS = 5


def reason_counts(reasons: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(reasons).items()))


@dataclass(frozen=True)
class CanonicalIssuer:
    issuer_id: str
    legacy_id: str
    ticker: str


@dataclass(frozen=True)
class UnmappedIssuer:
    legacy_id: str
    ticker: str
    reason: str = NO_CANONICAL_ISSUER_ID
    detail: str = ""


@dataclass(frozen=True)
class UnvisitedIssuer:
    issuer_id: str
    legacy_id: str
    ticker: str
    reason: str = HEAD_MEMBER_NOT_IN_UNIVERSE


@dataclass(frozen=True)
class CanonicalUniverse:
    issuers: tuple[CanonicalIssuer, ...]
    unmapped: tuple[UnmappedIssuer, ...] = ()
    unvisited: tuple[UnvisitedIssuer, ...] = ()
    wide_row_issuers: int = 0

    def tickers(self) -> dict[str, str]:
        return {issuer.issuer_id: issuer.ticker for issuer in self.issuers}

    def unmapped_by_reason(self) -> dict[str, int]:
        return reason_counts(member.reason for member in self.unmapped)

    def lane_failure(self) -> str | None:
        joined, wide = len(self.issuers), self.wide_row_issuers
        if joined == 0 and (wide or self.unmapped):
            return f"{joined} of {wide} wide-row issuers join"
        if wide >= JOIN_FLOOR_MIN_ISSUERS and joined < JOIN_FLOOR * wide:
            return f"{joined} of {wide} wide-row issuers join, below the floor of {JOIN_FLOOR}"
        return None


def is_canonical_issuer_id(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def require_canonical_issuer_id(value: str) -> str:
    if not is_canonical_issuer_id(value):
        raise ValueError(
            f"issuer id {value!r} is not the wide row's id (a lower-case UUID); resolve it with canonicalize_universe"
        )
    return value


def stored_entity_of(
    connection: psycopg.Connection[Any], legacy_id: str, *, as_of: date, known_at: datetime
) -> uuid.UUID | None:
    entity = lookup_entity(connection, legacy_id, "issuer", as_of=as_of, known_at=known_at)
    if entity is not None and is_uuid(legacy_id):
        held = connection.execute("select 1 from staging.entities where entity_id = %s", (entity,)).fetchone()
        return entity if held else None
    return entity


def canonicalize_universe(
    connection: psycopg.Connection[Any],
    tickers: Mapping[str, str] | Iterable[tuple[str, str]],
    *,
    cutoff: datetime,
    as_of: date,
    wide_row_ids: Collection[str],
) -> CanonicalUniverse:
    pairs = tickers.items() if isinstance(tickers, Mapping) else tickers
    issuers: dict[str, CanonicalIssuer] = {}
    unmapped: list[UnmappedIssuer] = []
    for legacy_id, ticker in pairs:
        entity = stored_entity_of(connection, legacy_id, as_of=as_of, known_at=cutoff)
        if entity is None:
            unmapped.append(UnmappedIssuer(legacy_id=legacy_id, ticker=ticker))
        elif str(entity) not in wide_row_ids:
            unmapped.append(UnmappedIssuer(legacy_id=legacy_id, ticker=ticker, reason=NOT_IN_WIDE_ROW))
        elif (eid := str(entity)) in issuers:
            current = issuers[eid]
            primary = choose_primary_listings(connection, {eid: [current.ticker, ticker]}, known_at=cutoff)
            if primary[eid] == ticker != current.ticker:
                issuers[eid] = CanonicalIssuer(issuer_id=eid, legacy_id=legacy_id, ticker=ticker)
                dropped_id, dropped_ticker, kept_id = current.legacy_id, current.ticker, legacy_id
            else:
                dropped_id, dropped_ticker, kept_id = legacy_id, ticker, current.legacy_id
            unmapped.append(
                UnmappedIssuer(
                    dropped_id, dropped_ticker, reason=DUPLICATE_CORPUS_ID, detail=f"same issuer as {kept_id}"
                )
            )
        else:
            issuers[str(entity)] = CanonicalIssuer(issuer_id=str(entity), legacy_id=legacy_id, ticker=ticker)
    return CanonicalUniverse(issuers=tuple(issuers.values()), unmapped=tuple(unmapped))


def _named_entities(
    connection: psycopg.Connection[Any], members: Iterable[UnmappedIssuer], *, known_at: datetime
) -> dict[str, str]:
    named: dict[str, str] = {}
    claimed: dict[tuple[str, str], list[str]] = {}
    for member in members:
        raw = member.legacy_id.strip()
        if is_uuid(raw):
            named.setdefault(raw.lower(), member.reason)
        for claim in {parse_alias(raw, "issuer"), ("legacy-id", raw)}:
            claimed.setdefault(claim, []).append(member.reason)
    if claimed:
        rows = connection.execute(
            """
            select claim.scheme, claim.value, a.entity_id::text, staging.entity_survivor(a.entity_id, %s)::text
            from staging.entity_aliases a
            join unnest(%s::text[], %s::text[]) as claim(scheme, value)
              on a.scheme = claim.scheme and a.value = claim.value
            """,
            (known_at, [scheme for scheme, _ in claimed], [value for _, value in claimed]),
        ).fetchall()
        for scheme, value, entity, survivor in rows:
            for named_id in (entity, survivor):
                if named_id is not None:
                    named.setdefault(named_id, claimed[(scheme, value)][0])
    return named


def _fill_reason(member_reason: str | None) -> str:
    if member_reason is None:
        return HEAD_MEMBER_NOT_IN_UNIVERSE
    return MEMBER_RESOLVES_ELSEWHERE if member_reason == NOT_IN_WIDE_ROW else member_reason


def account_for_head_members(
    connection: psycopg.Connection[Any],
    universe: CanonicalUniverse,
    *,
    cutoff: datetime,
    wide_row_ids: Collection[str],
) -> CanonicalUniverse:
    written = {issuer.issuer_id for issuer in universe.issuers}
    missing = sorted(set(wide_row_ids) - written)
    if not missing:
        return replace(universe, wide_row_issuers=len(wide_row_ids))
    named = _named_entities(connection, universe.unmapped, known_at=cutoff)
    rows = connection.execute(
        "select entity_id::text, current_ticker, legacy_id from mart.entity_identity where entity_id::text = any(%s)",
        (missing,),
    ).fetchall()
    labels = {entity_id: (ticker, legacy_id) for entity_id, ticker, legacy_id in rows}
    unvisited = tuple(
        UnvisitedIssuer(
            issuer_id=issuer_id,
            legacy_id=labels.get(issuer_id, (None, None))[1] or issuer_id,
            ticker=labels.get(issuer_id, (None, None))[0] or issuer_id[:8],
            reason=_fill_reason(named.get(issuer_id)),
        )
        for issuer_id in missing
    )
    return replace(universe, unvisited=unvisited, wide_row_issuers=len(wide_row_ids))


class ConflictingPredecessorError(ValueError):
    """Two signed registry rows name different predecessor CIKs for one issuer (#1257, #1263)."""


def expand_issuer_alias_ids(
    connection: psycopg.Connection[Any],
    issuer_by_key: Mapping[str, str],
    *,
    candidate_ids_by_key: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, tuple[str, ...]]:
    candidates: dict[str, set[str]] = {
        key: {*(candidate_ids_by_key.get(key, ()) if candidate_ids_by_key is not None else ()), issuer_id}
        for key, issuer_id in issuer_by_key.items()
    }
    uuid_ids = sorted({cid.lower() for cids in candidates.values() for cid in cids if is_uuid(cid)})
    if uuid_ids:
        alias_rows = connection.execute(
            """
            select entity_id::text, scheme, value
            from staging.entity_aliases
            where entity_id = any(%s::uuid[]) and scheme in ('legacy-id', 'lei')
            """,
            (uuid_ids,),
        ).fetchall()
        aliases_by_uuid: dict[str, set[str]] = {}
        for entity_id, scheme, value in alias_rows:
            if value:
                legacy_id = str(value) if scheme == "legacy-id" else f"issuer:lei:{value}"
                aliases_by_uuid.setdefault(str(entity_id).lower(), set()).add(legacy_id)
        for key, cids in candidates.items():
            expanded = set(cids)
            for cid in cids:
                if is_uuid(cid):
                    expanded.update(aliases_by_uuid.get(cid.lower(), ()))
            candidates[key] = expanded
    return {key: tuple(sorted(cids)) for key, cids in candidates.items()}


def signed_predecessor_ciks(
    connection: psycopg.Connection[Any],
    issuer_by_key: Mapping[str, str],
    *,
    candidate_ids_by_key: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, int]:
    if not issuer_by_key:
        return {}
    candidates = expand_issuer_alias_ids(connection, issuer_by_key, candidate_ids_by_key=candidate_ids_by_key)
    all_candidate_ids = sorted({cid for cids in candidates.values() for cid in cids})
    if not all_candidate_ids:
        return {}
    registry = dict(
        connection.execute(
            "select issuer_id, predecessor_cik from staging.issuer_cik_predecessors where issuer_id = any(%s)",
            (all_candidate_ids,),
        ).fetchall()
    )
    resolved: dict[str, int] = {}
    conflicts: dict[str, dict[int, str]] = {}
    for key, cids in candidates.items():
        named: dict[int, str] = {}
        for cid in cids:
            if cid in registry:
                named.setdefault(int(registry[cid]), cid)
        if len(named) > 1:
            conflicts[key] = named
        elif len(named) == 1:
            resolved[key] = next(iter(named))
    if conflicts:
        raise ConflictingPredecessorError(
            "signed predecessor rows disagree: "
            + "; ".join(
                f"{issuer_by_key[key]}: " + ", ".join(f"CIK {cik} under {named[cik]}" for cik in sorted(named))
                for key, named in sorted(conflicts.items())
            )
        )
    return resolved
