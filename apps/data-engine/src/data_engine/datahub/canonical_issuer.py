"""The wide row's issuer id for a universe member (#1079, docs/entity-identity.md §5).

The universe corpus names an issuer by a legacy id such as `issuer:lei:...`. The capture tick
resolves that id to an entity UUID, and the UUID is the `issuer_id` of `mart.topt_gppe_results`.
A mart row that the coverage report must join to the wide row stores that same UUID.

`canonicalize_universe` is the one resolver for those rows. It calls `lookup_entity`, which is
what `resolve_entity` calls first at capture time. It never mints.

The wide row stays the authority. A lookup repeats capture's question later, and evidence
recorded since can change its answer. A member whose answer is not a wide-row id gets no row.

Every wide-row issuer ends with one row. `account_for_head_members` lists each one that no
member joined, with the reason. The lane writes it an unavailable row, so the report shows why.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

import psycopg

from data_engine.datahub.resolve_coordinates import is_uuid, lookup_entity, parse_alias

__all__ = (
    "DUPLICATE_CORPUS_ID",
    "HEAD_MEMBER_NOT_IN_UNIVERSE",
    "JOIN_FLOOR",
    "JOIN_FLOOR_MIN_ISSUERS",
    "MEMBER_RESOLVES_ELSEWHERE",
    "NOT_IN_WIDE_ROW",
    "NO_CANONICAL_ISSUER_ID",
    "PRIMARY_INSTRUMENT_IDS",
    "PRIMARY_ISSUER_TICKERS",
    "SECONDARY_INSTRUMENT_IDS",
    "SECONDARY_ISSUER_TICKERS",
    "CanonicalIssuer",
    "CanonicalUniverse",
    "ConflictingPredecessorError",
    "UnmappedIssuer",
    "UnvisitedIssuer",
    "account_for_head_members",
    "canonicalize_universe",
    "expand_issuer_alias_ids",
    "instrument_preference_rank",
    "instrument_sort_key",
    "is_canonical_issuer_id",
    "is_preferred_issuer_ticker",
    "reason_counts",
    "require_canonical_issuer_id",
    "signed_predecessor_ciks",
    "stored_entity_of",
    "ticker_preference_rank",
    "ticker_sort_key",
)

#: Primary and secondary share-class tickers of multi-class issuers (#1265). The ticker states which
#: class is primary. Alphabet Inc. (5493006MHB84DD0ZWV18) has Class A voting stock (GOOGL, 02079K305)
#: and Class C non-voting stock (GOOG, 02079K107) in the universe corpus. All planes resolve to GOOGL.
#: Rank a listing by its ticker (`ticker_sort_key`), never by an id literal. Production holds entity
#: ids that the entity backfill minted, and a literal written in advance did not match them (#1347).
PRIMARY_ISSUER_TICKERS: frozenset[str] = frozenset({"GOOGL"})
SECONDARY_ISSUER_TICKERS: frozenset[str] = frozenset({"GOOG"})

#: The instrument ids of the two classes, as old captures and an empty test store spell them. Their
#: one reader is the TOPT core execution member (`production_topt.materialization`). Production
#: instrument ids are other entity ids, so there these sets match nothing (#1347). #1265 item 1 owns
#: the move of that reader to the ticker rule.
PRIMARY_INSTRUMENT_IDS: frozenset[str] = frozenset(
    {
        "security:cusip:02079K305",
        "security:figi:bbg009s39jy5",
        "729934dd-45ed-5de4-9b17-4f2df4a761e7",
    }
)
SECONDARY_INSTRUMENT_IDS: frozenset[str] = frozenset(
    {
        "security:cusip:02079K107",
        "security:figi:bbg009s3nb21",
        "94186fc0-31be-5b53-bd7c-8bd1000509b6",
    }
)


def ticker_preference_rank(ticker: str | None) -> int:
    """Return 0 for a primary class ticker, 2 for a secondary class ticker, and 1 otherwise."""
    if ticker in PRIMARY_ISSUER_TICKERS:
        return 0
    if ticker in SECONDARY_ISSUER_TICKERS:
        return 2
    return 1


def is_preferred_issuer_ticker(candidate: str | None, current: str | None) -> bool:
    """Return True when candidate outranks current for a multi-class issuer (#1265)."""
    return ticker_preference_rank(candidate) < ticker_preference_rank(current)


def ticker_sort_key(ticker: str | None) -> tuple[bool, int, str]:
    """The sort key of one listing of an issuer, by its ticker (#1265, #1347).

    The primary class comes first and the secondary class last (`ticker_preference_rank`). The ticker
    text breaks a tie between two classes of one rank. A listing without a known ticker comes after
    every listing with a ticker: no price series names it. Two ids of one ticker tie. The caller breaks
    that tie with a defined order of its own, for example the listing id.
    """
    return (ticker is None, ticker_preference_rank(ticker), ticker or "")


def instrument_preference_rank(instrument_id: str | None) -> int:
    """Return 0 for a primary instrument, 2 for a secondary instrument, and 1 otherwise."""
    if instrument_id in PRIMARY_INSTRUMENT_IDS:
        return 0
    if instrument_id in SECONDARY_INSTRUMENT_IDS:
        return 2
    return 1


def instrument_sort_key(instrument_id: str) -> tuple[int, str]:
    """Sort key that orders primary share-class instruments ahead of secondary instruments (#1265)."""
    return (instrument_preference_rank(instrument_id), instrument_id)


#: Why an issuer got no row: the store holds no entity for its legacy id.
NO_CANONICAL_ISSUER_ID = "no_canonical_issuer_id"
#: Why an issuer got no row: its entity is not an `issuer_id` of the head's wide row.
NOT_IN_WIDE_ROW = "not_in_wide_row"
#: Why a wide-row issuer got no row from a member: no member of the current universe names it.
HEAD_MEMBER_NOT_IN_UNIVERSE = "head_member_not_in_universe"
#: Why a member got no row: another member of the universe names the same issuer.
DUPLICATE_CORPUS_ID = "duplicate_corpus_id"
#: Why a wide-row issuer got an unavailable row: its member resolves to an entity outside the wide row.
#: The issuer is in the wide row, so the reason is not `not_in_wide_row`, which names the member.
MEMBER_RESOLVES_ELSEWHERE = "member_resolves_elsewhere"

#: The share of the wide row that members must join. Below it, the lane fails like a total drop.
JOIN_FLOOR = 0.5
#: The floor applies from this size of wide row. A smaller one fails only when nothing joins.
JOIN_FLOOR_MIN_ISSUERS = 5


def reason_counts(reasons: Iterable[str]) -> dict[str, int]:
    """How many times each reason occurs, in the order of the reasons. The one count of the lane."""
    return dict(sorted(Counter(reasons).items()))


@dataclass(frozen=True)
class CanonicalIssuer:
    """One universe member. `issuer_id` is the wide row's id and the only id a mart row stores.

    `legacy_id` is the id the corpus and the knowledge graph use. A read of the graph takes it.
    """

    issuer_id: str
    legacy_id: str
    ticker: str


@dataclass(frozen=True)
class UnmappedIssuer:
    """A member of the current universe that gets no row, with the reason."""

    legacy_id: str
    ticker: str
    reason: str = NO_CANONICAL_ISSUER_ID
    detail: str = ""


@dataclass(frozen=True)
class UnvisitedIssuer:
    """A wide-row issuer that no member joined. The lane writes it an unavailable row."""

    issuer_id: str
    legacy_id: str
    ticker: str
    reason: str = HEAD_MEMBER_NOT_IN_UNIVERSE


@dataclass(frozen=True)
class CanonicalUniverse:
    """The wide-row issuers that members joined, the members that got no row, and the issuers left over.

    `issuers` joined a member. `unvisited` is every other wide-row issuer.
    So `len(issuers) + len(unvisited)` equals `wide_row_issuers`. `unmapped` counts members only.
    """

    issuers: tuple[CanonicalIssuer, ...]
    unmapped: tuple[UnmappedIssuer, ...] = ()
    unvisited: tuple[UnvisitedIssuer, ...] = ()
    wide_row_issuers: int = 0

    def tickers(self) -> dict[str, str]:
        """Canonical issuer id to ticker, for a writer that never reads the graph."""
        return {issuer.issuer_id: issuer.ticker for issuer in self.issuers}

    def unmapped_by_reason(self) -> dict[str, int]:
        return reason_counts(member.reason for member in self.unmapped)

    def lane_failure(self) -> str | None:
        """The message when too few wide-row issuers join a member, else None.

        Nothing joins, or fewer than `JOIN_FLOOR` of a wide row of `JOIN_FLOOR_MIN_ISSUERS` or more.
        The report would then say `no_row` or the reason for most issuers. The text holds counts only.
        """
        joined, wide = len(self.issuers), self.wide_row_issuers
        if joined == 0 and (wide or self.unmapped):
            return f"{joined} of {wide} wide-row issuers join"
        if wide >= JOIN_FLOOR_MIN_ISSUERS and joined < JOIN_FLOOR * wide:
            return f"{joined} of {wide} wide-row issuers join, below the floor of {JOIN_FLOOR}"
        return None


def is_canonical_issuer_id(value: str) -> bool:
    """True for the lower-case hyphenated UUID text that the wide row stores."""
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def require_canonical_issuer_id(value: str) -> str:
    """`value` unchanged, or ValueError. The last check before a mart row is written."""
    if not is_canonical_issuer_id(value):
        raise ValueError(
            f"issuer id {value!r} is not the wide row's id (a lower-case UUID); resolve it with canonicalize_universe"
        )
    return value


def stored_entity_of(
    connection: psycopg.Connection[Any], legacy_id: str, *, as_of: date, known_at: datetime
) -> uuid.UUID | None:
    """The entity `legacy_id` names, or None. A UUID-form id must also be an entity of the store.

    `lookup_entity` answers a UUID with the UUID itself when the store holds no such entity. This
    guard is the one place that refuses it, for every resolver of issuer ids (#1079, #1257).
    """
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
    """Map each legacy id of `tickers` to its wide-row issuer id, or to a reason it has none.

    `as_of` is the date capture resolved with, the corpus report date. A claim that ended or
    began between that date and the cutoff names another entity under a later date.
    `known_at` is the head's cutoff, as at capture, so evidence knowable later stays unseen.
    Two legacy ids of one entity give one issuer: the first one keeps its place.
    The second is unmapped as `duplicate_corpus_id`.
    Raises when the store matches one id to two entities.
    """
    pairs = tickers.items() if isinstance(tickers, Mapping) else tickers
    issuers: dict[str, CanonicalIssuer] = {}
    unmapped: list[UnmappedIssuer] = []
    for legacy_id, ticker in pairs:
        entity = stored_entity_of(connection, legacy_id, as_of=as_of, known_at=cutoff)
        if entity is None:
            unmapped.append(UnmappedIssuer(legacy_id=legacy_id, ticker=ticker))
        elif str(entity) not in wide_row_ids:
            unmapped.append(UnmappedIssuer(legacy_id=legacy_id, ticker=ticker, reason=NOT_IN_WIDE_ROW))
        elif str(entity) in issuers:
            current = issuers[str(entity)]
            if is_preferred_issuer_ticker(ticker, current.ticker):
                issuers[str(entity)] = CanonicalIssuer(issuer_id=str(entity), legacy_id=legacy_id, ticker=ticker)
                unmapped.append(
                    UnmappedIssuer(
                        current.legacy_id,
                        current.ticker,
                        reason=DUPLICATE_CORPUS_ID,
                        detail=f"same issuer as {legacy_id}",
                    )
                )
            else:
                unmapped.append(
                    UnmappedIssuer(
                        legacy_id,
                        ticker,
                        reason=DUPLICATE_CORPUS_ID,
                        detail=f"same issuer as {current.legacy_id}",
                    )
                )
        else:
            issuers[str(entity)] = CanonicalIssuer(issuer_id=str(entity), legacy_id=legacy_id, ticker=ticker)
    return CanonicalUniverse(issuers=tuple(issuers.values()), unmapped=tuple(unmapped))


def _named_entities(
    connection: psycopg.Connection[Any], members: Iterable[UnmappedIssuer], *, known_at: datetime
) -> dict[str, str]:
    """The entities, and their survivors, that any alias of any dropped member ever named.

    Each maps to the reason its member was dropped. This is the claim a member makes, whatever
    date or evidence decides its lookup. It finds the wide-row issuer that a member names but
    resolves away from.
    """
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
    """The reason on the row of a wide-row issuer, from the reason its member was dropped for."""
    if member_reason is None:
        return HEAD_MEMBER_NOT_IN_UNIVERSE
    if member_reason == NOT_IN_WIDE_ROW:
        return MEMBER_RESOLVES_ELSEWHERE
    return member_reason


def account_for_head_members(
    connection: psycopg.Connection[Any],
    universe: CanonicalUniverse,
    *,
    cutoff: datetime,
    wide_row_ids: Collection[str],
) -> CanonicalUniverse:
    """List each wide-row issuer that no member joined, with the reason (#1079).

    The universe the lane lists is the current one, so the head can hold an issuer it lacks.
    Such an issuer gets reason `head_member_not_in_universe`. A member dropped for another
    reason names its own issuer, and that reason follows the issuer. A member that resolves
    outside the wide row gives `member_resolves_elsewhere`. The names come from the identity view.
    """
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
    """Return every candidate issuer identifier for each key, including legacy aliases of UUIDs (#1263).

    `staging.issuer_cik_predecessors` stores legacy identifiers such as `issuer:lei:...`.
    When a caller passes canonical entity UUIDs, this helper expands each UUID with its
    `legacy-id` and `lei` aliases from `staging.entity_aliases`.
    """
    candidates: dict[str, set[str]] = {}
    for key, issuer_id in issuer_by_key.items():
        initial = set(candidate_ids_by_key.get(key, ()) if candidate_ids_by_key is not None else ())
        initial.add(issuer_id)
        candidates[key] = initial

    uuid_ids = sorted({cid.lower() for cids in candidates.values() for cid in cids if is_uuid(cid)})
    if uuid_ids:
        alias_rows = connection.execute(
            """
            select entity_id::text, scheme, value
            from staging.entity_aliases
            where entity_id = any(%s::uuid[])
              and scheme in ('legacy-id', 'lei')
            """,
            (uuid_ids,),
        ).fetchall()
        aliases_by_uuid: dict[str, set[str]] = {}
        for entity_id, scheme, value in alias_rows:
            if not value:
                continue
            legacy_id = str(value) if scheme == "legacy-id" else f"issuer:lei:{value}"
            aliases_by_uuid.setdefault(str(entity_id).lower(), set()).add(legacy_id)
        for key, cids in candidates.items():
            expanded = set(cids)
            for cid in list(cids):
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
    """Resolve owner-signed predecessor CIKs across canonical UUIDs and their legacy aliases (#1263).

    Raises `ConflictingPredecessorError` when two signed rows for the same issuer disagree.
    """
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
