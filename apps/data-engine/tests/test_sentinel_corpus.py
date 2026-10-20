"""The sentinel replay corpus checks can fail (#1416 E7).

Each test changes one fact of a complete, fresh corpus and asserts the named finding. The corpus
here is synthetic; the standing test over the committed staging recording uses the same
`validate_corpus`. The recorder test runs the real SQL against a migrated Postgres.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

import pytest
from data_engine.datahub import sentinel_corpus as corpus
from data_engine.datahub.production_topt.source_registrations import (
    REGISTRATIONS,
    OriginRegistration,
    SourceRegistration,
)
from truealpha_contracts.models import DataSource

TODAY = date(2026, 10, 20)
NOW = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Sentinel:
    ticker: str
    issuer_id: str


# Two share classes of one issuer and a class-B ticker with punctuation: the shapes that differ.
SENTINELS = (
    Sentinel("GOOGL", "issuer:cik:0001652044"),
    Sentinel("GOOG", "issuer:cik:0001652044"),
    Sentinel("BRK.B", "issuer:cik:0001067983"),
)
ALL_ORIGINS = {
    "market-price": ("moomoo-kline:v1", "primary", "twelve-data:v1"),
    "financial-fact": ("moomoo-financials:v1", "primary"),
}
PRIMARY_ONLY = {"market-price": ("primary",), "financial-fact": ("primary",)}


def _record_id(kind: corpus.KeyKind, entity: str) -> str:
    # The shapes the adapters write (market_price_adapter, sec_financial_adapter).
    if kind.axis == "issuer":
        return f"{kind.record_prefix}:CIK{entity}"
    return f"{kind.record_prefix}:{entity}:2026-10-16"


def _files(
    sentinels=SENTINELS, origins=ALL_ORIGINS, *, last_used_at: datetime = NOW, environment: str = "staging"
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    entries = []
    for fetch_id, (key, kind) in enumerate(corpus.required_keys(sentinels, origins), start=1):
        body = f'{{"key": "{key.describe()}"}}'.encode()
        sha = hashlib.sha256(body).hexdigest()
        path = corpus.object_path(kind.source.value, sha)
        files[path] = corpus.compress(body)
        entries.append(
            corpus.CorpusEntry(
                semantic_type=key.semantic_type,
                origin=key.origin,
                entity=key.entity,
                source=kind.source.value,
                source_record_id=_record_id(kind, key.entity),
                raw_fetch_id=fetch_id,
                payload_sha256=sha,
                byte_length=len(body),
                content_type="application/json",
                fetched_at=last_used_at - timedelta(days=3),
                last_used_at=last_used_at,
                object_path=path,
            )
        )
    manifest = corpus.CorpusManifest(
        environment=environment,
        release="v0.0.0",
        recorded_at=NOW,
        configured_origins=dict(origins),
        entries=tuple(entries),
    )
    files[corpus.MANIFEST_NAME] = manifest.to_json()
    return files


def _manifest(files: dict[str, bytes]) -> corpus.CorpusManifest:
    return corpus.CorpusManifest.model_validate_json(files[corpus.MANIFEST_NAME])


def _with_entries(files: dict[str, bytes], entries) -> dict[str, bytes]:
    manifest = _manifest(files).model_copy(update={"entries": tuple(entries)})
    return {**files, corpus.MANIFEST_NAME: manifest.to_json()}


# -- the key kinds follow the registrations ----------------------------------------------------


def test_key_kinds_name_exactly_the_registered_vendor_origins() -> None:
    kinds = {(kind.semantic_type, kind.origin) for kind in corpus.KEY_KINDS}
    assert kinds == corpus.registered_key_origins()


def test_a_new_registered_origin_without_a_key_kind_is_detected() -> None:
    market = next(r for r in REGISTRATIONS if r.source_id == "yahoo-chart")
    extra = OriginRegistration(
        origin_source="another-vendor:v1",
        origin_id="origin:another-vendor:v1",
        value_key="close",
        parser_versions=("another-vendor-parser:v1",),
        ledger_seat="sec",
    )
    widened = replace(market, origins=(*market.origins, extra))
    pairs = corpus.registered_key_origins((widened, *REGISTRATIONS[1:]))
    kinds = {(kind.semantic_type, kind.origin) for kind in corpus.KEY_KINDS}
    assert pairs - kinds == {("market-price", "another-vendor:v1")}
    with pytest.raises(ValueError, match="another-vendor:v1"):
        corpus.required_keys(SENTINELS, {"market-price": ("another-vendor:v1",)})


def test_a_registration_without_a_vendor_seat_has_no_sentinel_keys() -> None:
    release = SourceRegistration(
        source_id="release-like",
        version="v1",
        semantic_types=("listing-identity",),
        freshness_max_age={"listing-identity": timedelta(days=365)},
        route_builder="x:y",
    )
    assert corpus.registered_key_origins((release,)) == frozenset()


def test_share_classes_share_one_company_facts_key_and_keep_their_symbol_keys() -> None:
    keys = [key for key, _ in corpus.required_keys(SENTINELS, ALL_ORIGINS)]
    facts = [k.entity for k in keys if (k.semantic_type, k.origin) == ("financial-fact", "primary")]
    moomoo = [k.entity for k in keys if (k.semantic_type, k.origin) == ("financial-fact", "moomoo-financials:v1")]
    assert sorted(facts) == ["0001067983", "0001652044"]
    assert sorted(moomoo) == ["BRK.B", "GOOG", "GOOGL"]
    # 3 listings x 3 price origins + 2 issuers + 3 symbols.
    assert len(keys) == 9 + 2 + 3


def test_record_ids_match_only_their_own_entity() -> None:
    chart = corpus.kind_for("market-price", "primary")
    facts = corpus.kind_for("financial-fact", "primary")
    assert chart is not None and facts is not None
    assert chart.matches("BRK.B", "chart:BRK.B:2026-10-16")
    assert not chart.matches("BRK.B", "chart:BRK.BX:2026-10-16")
    assert not chart.matches("GOOG", "chart:GOOGL:2026-10-16")
    assert not chart.matches("GOOG", "chart:GOOG:")
    assert facts.matches("0001652044", "companyfacts:CIK0001652044")
    assert not facts.matches("0001652044", "companyfacts:CIK00016520440")
    assert chart.record_pattern("A_B%") == "chart:A\\_B\\%:%"


def test_an_issuer_id_without_a_cik_is_refused() -> None:
    with pytest.raises(ValueError, match="10-digit CIK"):
        corpus.required_keys((Sentinel("X", "issuer:lei:ABC"),), PRIMARY_ONLY)


# -- the validator fails on each defect --------------------------------------------------------


def test_a_complete_fresh_staging_corpus_passes() -> None:
    assert corpus.validate_corpus(_files(), SENTINELS, today=TODAY) == []


def test_a_missing_sentinel_key_fails() -> None:
    files = _files()
    kept = [e for e in _manifest(files).entries if not (e.entity == "0001067983" and e.origin == "primary")]
    findings = corpus.validate_corpus(_with_entries(files, kept), SENTINELS, today=TODAY)
    assert findings == ["financial-fact/primary/0001067983: sentinel key is missing"]


def test_a_sentinel_added_to_the_set_without_a_recording_fails() -> None:
    wider = (*SENTINELS, Sentinel("CRWD", "issuer:cik:0001535527"))
    findings = corpus.validate_corpus(_files(), wider, today=TODAY)
    # Three price origins, one company-facts key and one moomoo-financials key.
    assert len(findings) == 5
    assert all("CRWD" in f or "0001535527" in f for f in findings)
    assert all(f.endswith("sentinel key is missing") for f in findings)


def test_a_key_outside_the_sentinel_set_fails() -> None:
    narrow = SENTINELS[:2]
    findings = corpus.validate_corpus(_files(), narrow, today=TODAY)
    assert findings
    assert all(f.endswith("not a sentinel key of the configured origins") for f in findings)
    assert {f.split("/")[2].split(":")[0] for f in findings} == {"BRK.B", "0001067983"}


def test_a_key_staging_last_used_more_than_90_days_ago_fails() -> None:
    at_bound = _files(last_used_at=NOW - timedelta(days=90))
    assert corpus.validate_corpus(at_bound, SENTINELS, today=TODAY) == []
    stale = _files(last_used_at=NOW - timedelta(days=91))
    findings = corpus.validate_corpus(stale, SENTINELS, today=TODAY)
    assert len(findings) == len(_manifest(stale).entries)
    assert all("older than 90 days" in f for f in findings)


def test_one_changed_byte_fails() -> None:
    files = _files()
    entry = _manifest(files).entries[0]
    body = bytearray(gzip.decompress(files[entry.object_path]))
    body[0] ^= 0x01
    tampered = {**files, entry.object_path: corpus.compress(bytes(body))}
    findings = corpus.validate_corpus(tampered, SENTINELS, today=TODAY)
    assert findings == [f"{entry.key.describe()}: bytes differ from the digest raw.fetches recorded"]


def test_a_missing_object_fails() -> None:
    files = _files()
    entry = _manifest(files).entries[-1]
    del files[entry.object_path]
    assert corpus.validate_corpus(files, SENTINELS, today=TODAY) == [
        f"{entry.key.describe()}: {entry.object_path} is missing"
    ]


def test_an_object_that_is_not_gzip_fails() -> None:
    files = _files()
    entry = _manifest(files).entries[0]
    files[entry.object_path] = b"plain bytes"
    assert corpus.validate_corpus(files, SENTINELS, today=TODAY) == [
        f"{entry.key.describe()}: {entry.object_path} is not a gzip file"
    ]


def test_a_record_id_of_another_entity_fails() -> None:
    files = _files()
    entries = list(_manifest(files).entries)
    index = next(i for i, e in enumerate(entries) if e.origin == "primary" and e.entity == "GOOG")
    entries[index] = entries[index].model_copy(update={"source_record_id": "chart:GOOGL:2026-10-16"})
    findings = corpus.validate_corpus(_with_entries(files, entries), SENTINELS, today=TODAY)
    assert findings == ["market-price/primary/GOOG: yahoo:chart:GOOGL:2026-10-16 is not a key of this kind"]


def test_a_recording_of_another_environment_fails() -> None:
    findings = corpus.validate_corpus(_files(environment="production"), SENTINELS, today=TODAY)
    assert findings == ["recorded in 'production', not 'staging'"]


def test_configured_origins_without_the_primary_fetch_fail() -> None:
    origins = {"market-price": ("twelve-data:v1",), "financial-fact": ("primary",)}
    findings = corpus.validate_corpus(_files(origins=origins), SENTINELS, today=TODAY)
    assert findings == ["market-price: the configured origins omit its primary fetch"]


def test_a_key_recorded_twice_fails() -> None:
    files = _files()
    entries = _manifest(files).entries
    findings = corpus.validate_corpus(_with_entries(files, (*entries, entries[0])), SENTINELS, today=TODAY)
    assert findings == [f"{entries[0].key.describe()}: recorded twice"]


def test_a_corpus_without_a_manifest_fails() -> None:
    assert corpus.validate_corpus({}, SENTINELS, today=TODAY) == ["manifest.v1.json is missing"]


# -- output shapes -----------------------------------------------------------------------------


def test_compressed_objects_are_byte_stable() -> None:
    assert corpus.compress(b"same body") == corpus.compress(b"same body")


def test_the_tar_and_the_directory_hold_the_same_files(tmp_path) -> None:
    files = _files()
    stream = io.BytesIO()
    corpus.write_corpus_tar(files, stream)
    stream.seek(0)
    with tarfile.open(fileobj=stream) as archive:
        from_tar = {m.name: archive.extractfile(m).read() for m in archive.getmembers()}  # type: ignore[union-attr]
    corpus.write_corpus_dir(files, tmp_path / "corpus")
    assert from_tar == files == corpus.read_corpus_dir(tmp_path / "corpus")
    with pytest.raises(FileExistsError):
        corpus.write_corpus_dir(files, tmp_path / "corpus")


# -- the recorder against a migrated Postgres --------------------------------------------------


@pytest.fixture
def conn():
    psycopg = pytest.importorskip("psycopg")
    from data_engine.config import settings
    from truealpha_runtime.testing import skip_or_fail

    try:
        connection = psycopg.connect(settings.database_url, connect_timeout=3)
    except psycopg.OperationalError:
        skip_or_fail("no reachable Postgres (make runtime-up && make db-migrate)")
    yield connection
    connection.rollback()
    connection.close()


def _land(conn, store, source: DataSource, record_id: str, body: bytes, fetched_at: datetime) -> int:
    from data_engine import raw_store

    return raw_store.insert_fetch(
        conn,
        source=source,
        source_record_id=record_id,
        body=body,
        content_type="application/json",
        fetched_at=fetched_at,
        store=store,
        recorded_at=fetched_at,
    )


def test_the_recorder_takes_the_newest_row_of_every_key_and_refuses_a_gap(conn) -> None:
    from truealpha_runtime.testing import InMemoryRawObjectStore

    store = InMemoryRawObjectStore()
    tag = uuid.uuid4().hex[:6].upper()
    sentinels = (Sentinel(f"Z{tag}.B", "issuer:cik:9000000001"), Sentinel(f"Z{tag}", "issuer:cik:9000000001"))
    day = NOW - timedelta(days=2)
    for sentinel in sentinels:
        _land(conn, store, DataSource.YAHOO, f"chart:{sentinel.ticker}:2026-10-15", b'{"old": 1}', day)
        _land(conn, store, DataSource.YAHOO, f"chart:{sentinel.ticker}:2026-10-16", b'{"new": 1}', NOW)
    # A ticker that extends the class-B ticker must not answer for it.
    _land(conn, store, DataSource.YAHOO, f"chart:Z{tag}.BX:2026-10-17", b'{"other": 1}', NOW)

    with pytest.raises(corpus.RecordingRefused) as refused:
        corpus.record_corpus(
            conn, store, sentinels, origins=PRIMARY_ONLY, environment="staging", release="v0", recorded_at=NOW
        )
    assert refused.value.findings == ("financial-fact/primary/9000000001: no sec row in raw.fetches",)

    _land(conn, store, DataSource.SEC, "companyfacts:CIK9000000001", b'{"facts": 1}', day)
    files = corpus.record_corpus(
        conn, store, sentinels, origins=PRIMARY_ONLY, environment="staging", release="v0", recorded_at=NOW
    )
    assert corpus.validate_corpus(files, sentinels, today=NOW.date()) == []
    entries = {e.key.describe(): e for e in _manifest(files).entries}
    assert sorted(entries) == sorted(
        [f"market-price/primary/{s.ticker}" for s in sentinels] + ["financial-fact/primary/9000000001"]
    )
    for sentinel in sentinels:
        price = entries[f"market-price/primary/{sentinel.ticker}"]
        assert price.source_record_id == f"chart:{sentinel.ticker}:2026-10-16"
        assert corpus.read_entry_body(files, price) == b'{"new": 1}'
    facts = entries["financial-fact/primary/9000000001"]
    # No capture vintage used these bytes, so the landing time is the last use.
    assert facts.last_used_at == facts.fetched_at == day


def test_the_recorder_refuses_a_key_staging_has_not_used_for_90_days(conn) -> None:
    from truealpha_runtime.testing import InMemoryRawObjectStore

    store = InMemoryRawObjectStore()
    tag = uuid.uuid4().hex[:6].upper()
    sentinels = (Sentinel(f"Y{tag}", "issuer:cik:9000000002"),)
    _land(conn, store, DataSource.YAHOO, f"chart:Y{tag}:2026-10-16", b'{"p": 1}', NOW)
    _land(conn, store, DataSource.SEC, "companyfacts:CIK9000000002", b'{"f": 1}', NOW - timedelta(days=120))
    with pytest.raises(corpus.RecordingRefused) as refused:
        corpus.record_corpus(
            conn, store, sentinels, origins=PRIMARY_ONLY, environment="staging", release="v0", recorded_at=NOW
        )
    assert len(refused.value.findings) == 1
    assert refused.value.findings[0].startswith("financial-fact/primary/9000000002: staging last used")


def _use_in_a_capture(conn, fetch_id: int, record_id: str, body: bytes, ticker: str) -> None:
    """Write the source vintage a staging tick writes when it reuses landed bytes."""
    from data_engine.datahub.repository import PostgresCaptureControlRepository
    from truealpha_contracts.common import canonical_sha256
    from truealpha_contracts.datahub import SourceRequest, SourceVintage
    from truealpha_contracts.universe import SubjectKind, SubjectRef

    repository = PostgresCaptureControlRepository(conn)
    request = SourceRequest(
        source_registry_entry_id=f"source-registry-entry:{canonical_sha256({'source': 'issue1416-e7:v1'})}",
        source_policy_id="source-policy:issue1416-e7-v1",
        request_fingerprint_version="issue1416-e7:v1",
        canonical_request_sha256=canonical_sha256({"record": record_id}),
        subject_refs=(SubjectRef(kind=SubjectKind.LISTING, id=f"listing:xnas:{ticker.lower()}"),),
        capture_requirement_ids=("capture-requirement:issue1416-e7",),
        partition="issue1416-e7",
    )
    repository.put_source_request(request)
    vintage = SourceVintage(
        source_request_id=request.source_request_id,
        source_record_id=record_id,
        source_published_at=None,
        raw_object_id=f"raw-object:{hashlib.sha256(body).hexdigest()}",
    )
    repository.put_source_vintage(vintage, raw_fetch_id=fetch_id)


def test_a_key_staging_reused_today_is_fresh_though_its_bytes_landed_long_ago(conn) -> None:
    """An unchanged re-fetch collapses onto the old raw.fetches row; the capture vintage dates the use."""
    from truealpha_runtime.testing import InMemoryRawObjectStore

    store = InMemoryRawObjectStore()
    tag = uuid.uuid4().hex[:6].upper()
    ticker, record_id, body = f"X{tag}", "companyfacts:CIK9000000003", b'{"facts": "unchanged"}'
    sentinels = (Sentinel(ticker, "issuer:cik:9000000003"),)
    now = datetime.now(UTC)
    _land(conn, store, DataSource.YAHOO, f"chart:{ticker}:2026-10-16", b'{"p": 2}', now)
    fetch_id = _land(conn, store, DataSource.SEC, record_id, body, now - timedelta(days=120))
    _use_in_a_capture(conn, fetch_id, record_id, body, ticker)

    files = corpus.record_corpus(
        conn, store, sentinels, origins=PRIMARY_ONLY, environment="staging", release="v0", recorded_at=now
    )
    facts = {e.key.describe(): e for e in _manifest(files).entries}["financial-fact/primary/9000000003"]
    assert facts.fetched_at == now - timedelta(days=120)
    assert facts.last_used_at > facts.fetched_at + timedelta(days=119)
