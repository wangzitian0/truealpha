"""Sentinel replay corpus: the recorded staging bytes of every sentinel key (#1416 E7).

Staging fetches the sentinel keys live. CI fetches nothing: it replays the bytes that a staging
run recorded. This module holds the three parts of that contract:

- the key kinds. One kind is a seat, an endpoint and the entity axis it keys on. The kinds
  follow the source registrations; a test fails when a registered origin has no kind, and a
  test drives the deployed adapters to prove each kind's record id;
- the recorder. It runs inside the staging code server. For each sentinel key it selects the
  `raw.fetches` row that staging used last, in a read-only transaction, reads the bytes through
  the object store and checks the digest that `raw.fetches` recorded. It refuses to write a
  corpus that the validator rejects;
- the validator. It rejects a corpus that lacks a sentinel key of any registered origin, holds
  a key that is not a sentinel key, holds bytes that differ from the recorded digest, or holds
  a key that staging last used more than `MAX_KEY_AGE` ago.

The last use of a row is the newest of three facts: the time its bytes first landed
(`raw.fetches.fetched_at`), the newest capture vintage that points at it
(`raw.capture_source_vintages.created_at`), and the newest successful vendor call that returned
the same bytes (`staging.api_call_ledger`, joined on `payload_sha256` as the gateway documents).
An unchanged re-fetch collapses onto the existing `raw.fetches` row and its vintage, so only
the ledger dates it.

Every registered origin is required, whatever the recording environment enables: staging reads
every source, and CI cannot replay an origin that staging does not fetch.

The sentinel set is not defined here. The recorder reads it from the environment contract, and
the standing corpus test passes the same set to `validate_corpus`.

Usage. Inside the staging code server (the image ships this module, not the scripts directory):
    /app/.venv/bin/python -m data_engine.datahub.sentinel_corpus record --tar > corpus.tar
In a checkout:
    uv run --package truealpha-data-engine python apps/data-engine/scripts/record_sentinel_corpus.py check <dir>
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import re
import sys
import tarfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from truealpha_contracts.common import CaptureEnvironment
from truealpha_contracts.models import DataSource
from truealpha_contracts.ports import RawObjectStore

from data_engine.datahub.production_topt.source_registrations import (
    MOOMOO_FINANCIALS_ORIGIN,
    MOOMOO_KLINE_ORIGIN,
    REGISTRATIONS,
    TWELVE_DATA_ORIGIN,
    SourceRegistration,
    configured_origin_sources,
)

MANIFEST_NAME = "manifest.v1.json"
#: The owner's bound (#1416 E7): a recorded staging run refreshes the corpus at least once per quarter.
MAX_KEY_AGE = timedelta(days=90)
#: The configured-origin id of a registration's own primary fetch (`configured_origin_sources`).
PRIMARY = "primary"
#: The corpus is a recording of this environment, never of production or a laptop.
RECORDED_ENVIRONMENT = CaptureEnvironment.STAGING.value

EntityAxis = Literal["listing", "issuer"]

_CIK = re.compile(r"^issuer:cik:(\d{10})$")


class SentinelRef(Protocol):
    """The two sentinel facts a key needs. The environment contract's sentinel members have both."""

    @property
    def ticker(self) -> str: ...

    @property
    def issuer_id(self) -> str: ...


@dataclass(frozen=True)
class KeyKind:
    """One family of sentinel fetch keys: the seat, the endpoint and the entity it keys on.

    ``record_prefix`` is the first segment of the `raw.fetches.source_record_id` that the
    adapter writes: ``chart:<ticker>:<session>`` (Yahoo), ``<origin>:<ticker>:<date>`` (the
    corroborating origins) and ``companyfacts:CIK<cik>`` (SEC company facts).
    """

    semantic_type: str
    origin: str
    source: DataSource
    record_prefix: str
    axis: EntityAxis

    def entity(self, sentinel: SentinelRef) -> str:
        if self.axis == "listing":
            return sentinel.ticker
        match = _CIK.match(sentinel.issuer_id)
        if match is None:
            raise ValueError(f"{sentinel.ticker}: issuer id {sentinel.issuer_id!r} carries no 10-digit CIK")
        return match.group(1)

    def record_pattern(self, entity: str) -> str:
        """The SQL LIKE pattern (escape character ``\\``) of this kind's record ids for ``entity``."""
        escaped = entity.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if self.axis == "issuer":
            return f"{self.record_prefix}:CIK{escaped}"
        return f"{self.record_prefix}:{escaped}:%"

    def matches(self, entity: str, source_record_id: str) -> bool:
        if self.axis == "issuer":
            return source_record_id == f"{self.record_prefix}:CIK{entity}"
        prefix = f"{self.record_prefix}:{entity}:"
        return source_record_id.startswith(prefix) and len(source_record_id) > len(prefix)


#: Every key kind the governed capture fetches per sentinel. `test_sentinel_corpus` proves that
#: this table and the vendor-seated origins of `REGISTRATIONS` name the same set, and that the
#: deployed adapters write record ids of these shapes.
KEY_KINDS: tuple[KeyKind, ...] = (
    KeyKind("market-price", PRIMARY, DataSource.YAHOO, "chart", "listing"),
    KeyKind("market-price", f"{TWELVE_DATA_ORIGIN}:v1", DataSource.TWELVE_DATA, TWELVE_DATA_ORIGIN, "listing"),
    KeyKind("market-price", f"{MOOMOO_KLINE_ORIGIN}:v1", DataSource.MOOMOO, MOOMOO_KLINE_ORIGIN, "listing"),
    KeyKind("financial-fact", PRIMARY, DataSource.SEC, "companyfacts", "issuer"),
    # moomoo financials key on the symbol, not the issuer (#1411 D5): GOOG and GOOGL are two keys.
    KeyKind("financial-fact", f"{MOOMOO_FINANCIALS_ORIGIN}:v1", DataSource.MOOMOO, MOOMOO_FINANCIALS_ORIGIN, "listing"),
)


def registered_key_origins(
    registrations: Sequence[SourceRegistration] = REGISTRATIONS,
) -> frozenset[tuple[str, str]]:
    """The ``(semantic_type, configured origin)`` pairs that vendor-seated registrations fetch.

    The origin on the registration's own seat is its primary fetch; every other origin is
    named as `configured_origin_sources` names it (``<origin source>``).
    """
    pairs: set[tuple[str, str]] = set()
    for registration in registrations:
        if registration.ledger_seat is None:
            continue
        for semantic_type in registration.semantic_types:
            pairs.add((semantic_type, PRIMARY))
            for origin in registration.origins:
                if origin.ledger_seat != registration.ledger_seat:
                    pairs.add((semantic_type, origin.origin_source))
    return frozenset(pairs)


def required_origins() -> dict[str, tuple[str, ...]]:
    """Every registered origin per vendor semantic type: the origins the corpus must hold."""
    origins: dict[str, list[str]] = {}
    for semantic_type, origin in sorted(registered_key_origins()):
        origins.setdefault(semantic_type, []).append(origin)
    return {semantic_type: tuple(names) for semantic_type, names in origins.items()}


def kind_for(semantic_type: str, origin: str) -> KeyKind | None:
    for kind in KEY_KINDS:
        if kind.semantic_type == semantic_type and kind.origin == origin:
            return kind
    return None


def configured_origins() -> dict[str, tuple[str, ...]]:
    """The origins this process's environment fetches, per vendor semantic type."""
    return {semantic: tuple(sorted(configured_origin_sources(semantic))) for semantic in required_origins()}


@dataclass(frozen=True, order=True)
class RequiredKey:
    semantic_type: str
    origin: str
    entity: str

    def describe(self) -> str:
        return f"{self.semantic_type}/{self.origin}/{self.entity}"


def required_keys(
    sentinels: Sequence[SentinelRef], origins: Mapping[str, Sequence[str]] | None = None
) -> tuple[tuple[RequiredKey, KeyKind], ...]:
    """Every sentinel key of ``origins`` (default: every registered origin), once each.

    GOOG and GOOGL share one CIK, so they share one company-facts key.
    """
    found: dict[RequiredKey, KeyKind] = {}
    for semantic_type, names in sorted((required_origins() if origins is None else origins).items()):
        for origin in sorted(names):
            kind = kind_for(semantic_type, origin)
            if kind is None:
                raise ValueError(f"{semantic_type}: origin {origin!r} has no key kind; add it to KEY_KINDS")
            for sentinel in sentinels:
                found.setdefault(RequiredKey(semantic_type, origin, kind.entity(sentinel)), kind)
    return tuple(sorted(found.items()))


# -- manifest ---------------------------------------------------------------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CorpusEntry(_Frozen):
    semantic_type: str
    origin: str
    entity: str
    source: str
    source_record_id: str
    raw_fetch_id: int
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)
    content_type: str
    fetched_at: AwareDatetime
    last_used_at: AwareDatetime
    object_path: str

    @property
    def key(self) -> RequiredKey:
        return RequiredKey(self.semantic_type, self.origin, self.entity)


class CorpusManifest(_Frozen):
    schema_version: Literal[1] = 1
    environment: str
    release: str = Field(min_length=1)
    recorded_at: AwareDatetime
    #: What the recording environment enabled, as evidence; the required origins do not come from here.
    configured_origins: dict[str, tuple[str, ...]]
    entries: tuple[CorpusEntry, ...]

    def to_json(self) -> bytes:
        return (self.model_dump_json(indent=2) + "\n").encode()


def object_path(source: str, payload_sha256: str) -> str:
    return f"objects/{source}/{payload_sha256}.gz"


def compress(body: bytes) -> bytes:
    """Gzip with a zero timestamp, so one body always gives the same file bytes."""
    return gzip.compress(body, compresslevel=9, mtime=0)


def _utc_date(moment: datetime) -> date:
    return moment.astimezone(UTC).date()


# -- validator --------------------------------------------------------------------------------


def validate_corpus(files: Mapping[str, bytes], sentinels: Sequence[SentinelRef], *, today: date) -> list[str]:
    """Return every reason the corpus fails; an empty list means it passes.

    ``files`` maps a corpus-relative path to its bytes (`read_corpus_dir` gives it for a directory).
    """
    if not sentinels:
        return ["the sentinel set is empty"]
    if MANIFEST_NAME not in files:
        return [f"{MANIFEST_NAME} is missing"]
    manifest = CorpusManifest.model_validate_json(files[MANIFEST_NAME])
    findings: list[str] = []
    if manifest.environment != RECORDED_ENVIRONMENT:
        findings.append(f"recorded in {manifest.environment!r}, not {RECORDED_ENVIRONMENT!r}")
    for semantic_type, names in required_origins().items():
        for origin in names:
            if origin not in manifest.configured_origins.get(semantic_type, ()):
                findings.append(f"{semantic_type}/{origin}: the recording environment does not fetch this origin")
    required = dict(required_keys(sentinels))

    by_key: dict[RequiredKey, CorpusEntry] = {}
    for entry in manifest.entries:
        if entry.key in by_key:
            findings.append(f"{entry.key.describe()}: recorded twice")
        by_key[entry.key] = entry
    for key in sorted(set(required) - set(by_key)):
        findings.append(f"{key.describe()}: sentinel key is missing")
    for key in sorted(set(by_key) - set(required)):
        findings.append(f"{key.describe()}: not a sentinel key of a registered origin")

    oldest = today - MAX_KEY_AGE
    for key in sorted(set(by_key) & set(required)):
        entry, kind = by_key[key], required[key]
        findings.extend(_entry_findings(entry, kind, files, oldest=oldest, recorded_at=manifest.recorded_at))
    return findings


def _entry_findings(
    entry: CorpusEntry, kind: KeyKind, files: Mapping[str, bytes], *, oldest: date, recorded_at: datetime
) -> list[str]:
    name = entry.key.describe()
    findings: list[str] = []
    if entry.source != kind.source.value or not kind.matches(entry.entity, entry.source_record_id):
        findings.append(f"{name}: {entry.source}:{entry.source_record_id} is not a key of this kind")
    if not entry.fetched_at <= entry.last_used_at <= recorded_at:
        findings.append(f"{name}: last use {entry.last_used_at} is not between landing and recording")
    if _utc_date(entry.last_used_at) < oldest:
        findings.append(
            f"{name}: staging last used these bytes on {_utc_date(entry.last_used_at)}, "
            f"older than {MAX_KEY_AGE.days} days; record the corpus again"
        )
    if entry.object_path != object_path(entry.source, entry.payload_sha256):
        findings.append(f"{name}: object path {entry.object_path} is not content-addressed")
    stored = files.get(entry.object_path)
    if stored is None:
        return [*findings, f"{name}: {entry.object_path} is missing"]
    try:
        body = gzip.decompress(stored)
    except (OSError, EOFError):
        return [*findings, f"{name}: {entry.object_path} is not a gzip file"]
    if len(body) != entry.byte_length or hashlib.sha256(body).hexdigest() != entry.payload_sha256:
        findings.append(f"{name}: bytes differ from the digest raw.fetches recorded")
    return findings


def read_corpus_dir(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes() for path in directory.rglob("*") if path.is_file()
    }


def read_entry_body(files: Mapping[str, bytes], entry: CorpusEntry) -> bytes:
    """The exact bytes staging fetched for ``entry`` (what a replay test parses)."""
    return gzip.decompress(files[entry.object_path])


# -- recorder ---------------------------------------------------------------------------------

# One query per key. The candidates are every row of the key; each gets its last use, and the
# row staging used last wins. The ledger join is the gateway's documented join key.
_LAST_USED_FETCH_SQL = """
with candidates as (
    select id, source, source_record_id, payload_sha256, byte_length, content_type, fetched_at
    from raw.fetches
    where source = %(source)s and source_record_id like %(pattern)s escape '\\'
),
vintage_use as (
    select v.raw_fetch_id, max(v.created_at) as used_at
    from raw.capture_source_vintages v
    where v.raw_fetch_id in (select id from candidates)
    group by v.raw_fetch_id
),
call_use as (
    select l.payload_sha256, max(l.called_at) as used_at
    from staging.api_call_ledger l
    where l.source = %(source)s and l.ok and l.payload_sha256 in (select payload_sha256 from candidates)
    group by l.payload_sha256
)
select c.id, c.source, c.source_record_id, c.payload_sha256, c.byte_length, c.content_type, c.fetched_at,
       greatest(c.fetched_at, coalesce(vu.used_at, c.fetched_at), coalesce(cu.used_at, c.fetched_at))
           as last_used_at
from candidates c
left join vintage_use vu on vu.raw_fetch_id = c.id
left join call_use cu on cu.payload_sha256 = c.payload_sha256
order by last_used_at desc, c.fetched_at desc, c.id desc
limit 1
"""


class RecordingRefused(RuntimeError):
    def __init__(self, findings: Sequence[str]) -> None:
        super().__init__("; ".join(findings))
        self.findings = tuple(findings)


def record_corpus(
    conn: Any,
    store: RawObjectStore,
    sentinels: Sequence[SentinelRef],
    *,
    origins: Mapping[str, Sequence[str]],
    environment: str,
    release: str,
    recorded_at: datetime,
) -> dict[str, bytes]:
    """Read the bytes staging used last for every sentinel key; return the corpus files.

    ``origins`` is what the recording environment enables (`configured_origins`), kept in the
    manifest as evidence. Raises `RecordingRefused` when a key has no row, its bytes cannot be
    read back, or the result fails `validate_corpus`. Nothing is written here.
    """
    from data_engine import raw_store

    if recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be timezone-aware")
    files: dict[str, bytes] = {}
    entries: list[CorpusEntry] = []
    missing: list[str] = []
    for key, kind in required_keys(sentinels):
        row = conn.execute(
            _LAST_USED_FETCH_SQL, {"source": kind.source.value, "pattern": kind.record_pattern(key.entity)}
        ).fetchone()
        if row is None:
            missing.append(f"{key.describe()}: no {kind.source.value} row in raw.fetches")
            continue
        fetch_id, source, record_id, sha256, byte_length, content_type, fetched_at, last_used_at = row
        try:
            body = raw_store.get_payload(conn, fetch_id, store=store)
        except Exception as error:  # noqa: BLE001 - every read failure is a named refusal, never a partial corpus
            missing.append(f"{key.describe()}: cannot read raw.fetches:{fetch_id} back: {error}")
            continue
        if len(body) != byte_length or hashlib.sha256(body).hexdigest() != sha256:
            missing.append(f"{key.describe()}: object bytes differ from raw.fetches:{fetch_id}")
            continue
        path = object_path(source, sha256)
        files[path] = compress(body)
        entries.append(
            CorpusEntry(
                semantic_type=key.semantic_type,
                origin=key.origin,
                entity=key.entity,
                source=source,
                source_record_id=record_id,
                raw_fetch_id=fetch_id,
                payload_sha256=sha256,
                byte_length=byte_length,
                content_type=content_type,
                fetched_at=fetched_at,
                last_used_at=last_used_at,
                object_path=path,
            )
        )
    if missing:
        raise RecordingRefused(missing)
    manifest = CorpusManifest(
        environment=environment,
        release=release,
        recorded_at=recorded_at,
        configured_origins={semantic: tuple(sorted(names)) for semantic, names in origins.items()},
        entries=tuple(entries),
    )
    files[MANIFEST_NAME] = manifest.to_json()
    findings = validate_corpus(files, sentinels, today=_utc_date(recorded_at))
    if findings:
        raise RecordingRefused(findings)
    return files


def write_corpus_dir(files: Mapping[str, bytes], directory: Path) -> None:
    """Write ``files`` into an empty or absent ``directory``; refuse to mix two recordings."""
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError(f"{directory} is not empty; remove the old recording first")
    for name, content in sorted(files.items()):
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def write_corpus_tar(files: Mapping[str, bytes], stream: io.BufferedIOBase | Any) -> None:
    """Write ``files`` as an uncompressed tar (the objects are gzip files already)."""
    with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for name, content in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))


def _contract_sentinels() -> Sequence[SentinelRef]:
    from truealpha_contracts.environment_contract import load_environment_contract

    return load_environment_contract().sentinels


def _record(args: argparse.Namespace) -> int:
    import psycopg

    from data_engine import raw_store
    from data_engine.config import settings

    environment = settings.capture_environment.value
    if environment != RECORDED_ENVIRONMENT:
        print(f"refused: this process runs in {environment!r}; record the corpus in staging", file=sys.stderr)
        return 2
    if not settings.git_commit_sha:
        print("refused: GIT_COMMIT_SHA is empty; the recording must name the release it ran", file=sys.stderr)
        return 2
    with psycopg.connect(settings.database_url, options="-c default_transaction_read_only=on") as conn:
        conn.execute("set statement_timeout = '60s'")
        # The database clock: vintage and landing times come from it, so the recording time does too.
        clock = conn.execute("select now()").fetchone()
        if clock is None:
            raise RuntimeError("select now() returned no row")
        recorded_at = clock[0]
        try:
            files = record_corpus(
                conn,
                raw_store.object_store(),
                _contract_sentinels(),
                origins=configured_origins(),
                environment=environment,
                release=settings.git_commit_sha,
                recorded_at=recorded_at,
            )
        except RecordingRefused as refused:
            for finding in refused.findings:
                print(f"refused: {finding}", file=sys.stderr)
            return 1
    if args.tar:
        write_corpus_tar(files, sys.stdout.buffer)
    else:
        write_corpus_dir(files, Path(args.out))
    return 0


def _check(args: argparse.Namespace) -> int:
    findings = validate_corpus(
        read_corpus_dir(Path(args.corpus)), _contract_sentinels(), today=datetime.now(UTC).date()
    )
    for finding in findings:
        print(f"FAIL {finding}", file=sys.stderr)
    return 1 if findings else 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="record the bytes staging used last for every sentinel key")
    target = record.add_mutually_exclusive_group(required=True)
    target.add_argument("--out", help="write the corpus into this empty directory")
    target.add_argument("--tar", action="store_true", help="write the corpus as a tar to stdout")
    record.set_defaults(handler=_record)
    check = commands.add_parser("check", help="validate a corpus directory against the sentinel set")
    check.add_argument("corpus")
    check.set_defaults(handler=_check)
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
