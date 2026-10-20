"""The sentinel replay corpus checks can fail (#1416 E7).

Each validator test changes one fact of a complete, fresh corpus and asserts the named finding.
The corpus here is synthetic; the standing test over the committed staging recording uses the
same `validate_corpus`. The key-kind tests drive the deployed adapters. The recorder tests run
the real SQL against a migrated Postgres.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from data_engine.datahub import sentinel_corpus as corpus
from data_engine.datahub.production_topt.source_registrations import (
    REGISTRATIONS,
    OriginRegistration,
    SourceRegistration,
)
from pydantic import ValidationError
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
ALL_ORIGINS = corpus.required_origins()


def _record_id(kind: corpus.KeyKind, entity: str) -> str:
    if kind.axis == "issuer":
        return f"{kind.record_prefix}:CIK{entity}"
    return f"{kind.record_prefix}:{entity}:2026-10-16"


def _files(
    sentinels=SENTINELS,
    *,
    last_used_at: datetime = NOW,
    environment: str = "staging",
    configured=ALL_ORIGINS,
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    entries = []
    for fetch_id, (key, kind) in enumerate(corpus.required_keys(sentinels), start=1):
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
        configured_origins=dict(configured),
        entries=tuple(entries),
    )
    files[corpus.MANIFEST_NAME] = manifest.to_json()
    return files


def _manifest(files: dict[str, bytes]) -> corpus.CorpusManifest:
    return corpus.CorpusManifest.model_validate_json(files[corpus.MANIFEST_NAME])


def _with_entries(files: dict[str, bytes], entries) -> dict[str, bytes]:
    manifest = _manifest(files).model_copy(update={"entries": tuple(entries)})
    return {**files, corpus.MANIFEST_NAME: manifest.to_json()}


# -- the key kinds follow the registrations and the deployed adapters ---------------------------


def test_key_kinds_name_exactly_the_registered_vendor_origins() -> None:
    kinds = {(kind.semantic_type, kind.origin) for kind in corpus.KEY_KINDS}
    assert kinds == corpus.registered_key_origins()
    assert ALL_ORIGINS == {
        "financial-fact": ("moomoo-financials:v1", "primary"),
        "market-price": ("moomoo-kline:v1", "primary", "twelve-data:v1"),
    }


def test_key_kinds_match_the_record_ids_the_deployed_adapters_write(monkeypatch) -> None:
    """Each kind against the code that writes the row, not against a copy of the kind table."""
    from data_engine.config import settings
    from data_engine.datahub.production_topt import moomoo_origin, twelve_data_origin
    from data_engine.datahub.production_topt.executor import FetchSuccess
    from data_engine.datahub.production_topt.market_price_adapter import (
        MarketPriceAdapter,
        MarketPriceQuote,
        MarketPriceTarget,
    )
    from data_engine.datahub.production_topt.sec_financial_adapter import company_facts_record_id
    from truealpha_contracts.datahub import CaptureWorkItem

    monkeypatch.setattr(settings, "twelve_data_api_key", "test-key-not-used")
    monkeypatch.setattr(settings, "moomoo_kline_origin_enabled", True)
    monkeypatch.setattr(settings, "moomoo_financials_origin_enabled", True)
    monkeypatch.setattr(moomoo_origin, "_require_opend", lambda: None)
    session = date(2026, 10, 16)
    quote = MarketPriceQuote(
        raw_bytes=b"{}", close=Decimal("1"), as_of=session, knowable_at=datetime(2026, 10, 16, tzinfo=UTC)
    )
    target = MarketPriceTarget("BRK.B", session, "issuer:x", "security:y", "listing:xnys:brk.b")
    item = CaptureWorkItem(
        campaign_id="capture-campaign:" + "1" * 64,
        source_request_id="source-request:" + "3" * 64,
        schedule_policy_id="schedule-policy:" + "2" * 64,
    )

    primary = MarketPriceAdapter({item.work_item_id: target}, lambda symbol, cutoff: quote).fetch(item)
    assert isinstance(primary, FetchSuccess)
    chart = corpus.kind_for("market-price", "primary")
    assert chart is not None and primary.raw.source is chart.source
    assert chart.matches("BRK.B", primary.raw.record_id)

    price_origins = (twelve_data_origin.twelve_data_origin(), moomoo_origin.moomoo_kline_origin())
    for origin in price_origins:
        assert origin is not None
        kind = corpus.kind_for("market-price", f"{origin.origin}:v1")
        assert kind is not None, origin.origin
        raw = MarketPriceAdapter._raw(origin, target, quote)
        assert raw.source is kind.source and kind.matches("BRK.B", raw.record_id), raw

    financials = moomoo_origin.moomoo_financials_origin()
    assert financials is not None
    kind = corpus.kind_for("financial-fact", f"{financials.origin}:v1")
    # The SEC adapter writes `<origin>:<ticker>:<period end>` under the origin's raw source.
    assert kind is not None and (kind.record_prefix, kind.source) == (financials.origin, financials.raw_source)

    facts = corpus.kind_for("financial-fact", "primary")
    assert facts is not None and facts.source is DataSource.SEC
    assert facts.matches("0001067983", company_facts_record_id(1067983))


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
    keys = [key for key, _ in corpus.required_keys(SENTINELS)]
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
        corpus.required_keys((Sentinel("X", "issuer:lei:ABC"),))


# -- the validator fails on each defect --------------------------------------------------------


def test_a_complete_fresh_staging_corpus_passes() -> None:
    assert corpus.validate_corpus(_files(), SENTINELS, today=TODAY) == []


def test_an_empty_sentinel_set_fails() -> None:
    assert corpus.validate_corpus(_files(), (), today=TODAY) == ["the sentinel set is empty"]


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
    findings = corpus.validate_corpus(_files(), SENTINELS[:2], today=TODAY)
    assert findings
    assert all(f.endswith("not a sentinel key of a registered origin") for f in findings)
    assert {f.split("/")[2].split(":")[0] for f in findings} == {"BRK.B", "0001067983"}


def test_an_origin_the_recording_environment_does_not_fetch_fails() -> None:
    """A staging without the Twelve Data key still holds older rows; the corpus must say so."""
    configured = {**ALL_ORIGINS, "market-price": ("moomoo-kline:v1", "primary")}
    findings = corpus.validate_corpus(_files(configured=configured), SENTINELS, today=TODAY)
    assert findings == ["market-price/twelve-data:v1: the recording environment does not fetch this origin"]


def test_a_key_staging_last_used_more_than_90_days_ago_fails() -> None:
    at_bound = _files(last_used_at=NOW - timedelta(days=90))
    assert corpus.validate_corpus(at_bound, SENTINELS, today=TODAY) == []
    stale = _files(last_used_at=NOW - timedelta(days=91))
    findings = corpus.validate_corpus(stale, SENTINELS, today=TODAY)
    assert len(findings) == len(_manifest(stale).entries)
    assert all("older than 90 days" in f for f in findings)


def test_the_age_bound_reads_the_utc_date() -> None:
    """21:00 on 2026-07-21 at UTC-5 is 02:00 on 2026-07-22 UTC: inside the 90-day bound."""
    files = _files(last_used_at=datetime(2026, 7, 21, 21, 0, tzinfo=timezone(timedelta(hours=-5))))
    assert corpus.validate_corpus(files, SENTINELS, today=TODAY) == []


def test_a_last_use_outside_landing_and_recording_fails() -> None:
    files = _files()
    entries = list(_manifest(files).entries)
    future = entries[0].model_copy(update={"last_used_at": NOW + timedelta(hours=1)})
    early = entries[1].model_copy(update={"last_used_at": entries[1].fetched_at - timedelta(seconds=1)})
    findings = corpus.validate_corpus(_with_entries(files, [future, early, *entries[2:]]), SENTINELS, today=TODAY)
    assert len(findings) == 2
    assert all("is not between landing and recording" in f for f in findings)


def test_a_naive_time_is_refused() -> None:
    entry = _manifest(_files()).entries[0].model_dump()
    entry["last_used_at"] = "2026-10-20T06:00:00"
    with pytest.raises(ValidationError):
        corpus.CorpusEntry.model_validate(entry)


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


@pytest.fixture
def store():
    from truealpha_runtime.testing import InMemoryRawObjectStore

    return InMemoryRawObjectStore()


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


def _land_every_key(conn, store, sentinels, at: datetime, *, skip: frozenset[str] = frozenset()) -> None:
    for key, kind in corpus.required_keys(sentinels):
        if key.describe() not in skip:
            body = f'{{"bytes": "{key.describe()}"}}'.encode()
            _land(conn, store, kind.source, _record_id(kind, key.entity), body, at)


def _call(conn, source: DataSource, body: bytes, called_at: datetime, *, ok: bool = True) -> None:
    """One gateway ledger row, as `sources.gateway` writes it for a vendor answer."""
    conn.execute(
        "insert into staging.api_call_ledger (source, endpoint, called_at, caller, ok, payload_sha256, byte_length)"
        " values (%s, 'test', %s, 'test_sentinel_corpus', %s, %s, %s)",
        (source.value, called_at, ok, hashlib.sha256(body).hexdigest(), len(body)),
    )


def _record(conn, store, sentinels, recorded_at: datetime = NOW) -> dict[str, bytes]:
    return corpus.record_corpus(
        conn, store, sentinels, origins=ALL_ORIGINS, environment="staging", release="v0", recorded_at=recorded_at
    )


def _sentinels(prefix: str, cik: str) -> tuple[Sentinel, ...]:
    tag = uuid.uuid4().hex[:6].upper()
    return (Sentinel(f"{prefix}{tag}.B", f"issuer:cik:{cik}"), Sentinel(f"{prefix}{tag}", f"issuer:cik:{cik}"))


def test_the_recorder_takes_every_key_and_refuses_a_gap(conn, store) -> None:
    sentinels = _sentinels("Z", "9000000001")
    day = NOW - timedelta(days=2)
    _land_every_key(conn, store, sentinels, NOW, skip=frozenset({"financial-fact/primary/9000000001"}))
    for sentinel in sentinels:
        _land(conn, store, DataSource.YAHOO, f"chart:{sentinel.ticker}:2026-10-15", b'{"old": 1}', day)
    # A ticker that extends the class-B ticker must not answer for it.
    _land(conn, store, DataSource.YAHOO, f"chart:{sentinels[0].ticker}X:2026-10-17", b'{"other": 1}', NOW)

    with pytest.raises(corpus.RecordingRefused) as refused:
        _record(conn, store, sentinels)
    assert refused.value.findings == ("financial-fact/primary/9000000001: no sec row in raw.fetches",)

    _land(conn, store, DataSource.SEC, "companyfacts:CIK9000000001", b'{"facts": 1}', day)
    files = _record(conn, store, sentinels)
    assert corpus.validate_corpus(files, sentinels, today=NOW.date()) == []
    entries = {e.key.describe(): e for e in _manifest(files).entries}
    assert len(entries) == 2 * 3 + 1 + 2
    for sentinel in sentinels:
        price = entries[f"market-price/primary/{sentinel.ticker}"]
        assert price.source_record_id == f"chart:{sentinel.ticker}:2026-10-16"
        assert corpus.read_entry_body(files, price) == f'{{"bytes": "{price.key.describe()}"}}'.encode()
    facts = entries["financial-fact/primary/9000000001"]
    # No vintage and no ledger call used these bytes again, so the landing time is the last use.
    assert facts.last_used_at == facts.fetched_at == day


def test_an_unchanged_re_fetch_dates_the_old_row_through_the_ledger(conn, store) -> None:
    """Same bytes again collapse onto the old raw.fetches row; only the ledger call is new."""
    sentinels = _sentinels("X", "9000000003")
    _land_every_key(conn, store, sentinels, NOW, skip=frozenset({"financial-fact/primary/9000000003"}))
    body = b'{"facts": "unchanged for a year"}'
    landed = NOW - timedelta(days=120)
    first = _land(conn, store, DataSource.SEC, "companyfacts:CIK9000000003", body, landed)
    assert _land(conn, store, DataSource.SEC, "companyfacts:CIK9000000003", body, NOW - timedelta(days=1)) == first

    # A failed call and a call that returned other bytes date nothing.
    _call(conn, DataSource.SEC, body, NOW - timedelta(hours=2), ok=False)
    _call(conn, DataSource.SEC, b'{"facts": "other"}', NOW - timedelta(hours=1))
    with pytest.raises(corpus.RecordingRefused) as refused:
        _record(conn, store, sentinels)
    assert len(refused.value.findings) == 1
    assert refused.value.findings[0].startswith("financial-fact/primary/9000000003: staging last used")

    _call(conn, DataSource.SEC, body, NOW - timedelta(days=1))
    facts = {e.key.describe(): e for e in _manifest(_record(conn, store, sentinels)).entries}[
        "financial-fact/primary/9000000003"
    ]
    assert facts.fetched_at == landed
    assert facts.last_used_at == NOW - timedelta(days=1)


def test_the_recorder_takes_the_row_staging_used_last_not_the_row_that_landed_last(conn, store) -> None:
    """Enriched and plain company facts alternate under one record id (#1347 share classes)."""
    sentinels = _sentinels("W", "9000000004")
    _land_every_key(conn, store, sentinels, NOW, skip=frozenset({"financial-fact/primary/9000000004"}))
    enriched, plain = b'{"facts": "enriched"}', b'{"facts": "plain"}'
    _land(conn, store, DataSource.SEC, "companyfacts:CIK9000000004", enriched, NOW - timedelta(days=30))
    _land(conn, store, DataSource.SEC, "companyfacts:CIK9000000004", plain, NOW - timedelta(days=20))
    _call(conn, DataSource.SEC, enriched, NOW - timedelta(hours=3))
    files = _record(conn, store, sentinels)
    facts = {e.key.describe(): e for e in _manifest(files).entries}["financial-fact/primary/9000000004"]
    assert corpus.read_entry_body(files, facts) == enriched


def _use_in_a_capture(conn, fetch_id: int, record_id: str, body: bytes, ticker: str) -> None:
    """Write the source vintage a staging tick writes for the bytes it captured."""
    from data_engine.datahub.repository import PostgresCaptureControlRepository
    from truealpha_contracts.common import canonical_sha256
    from truealpha_contracts.datahub import SourceRequest, SourceVintage
    from truealpha_contracts.universe import SubjectKind, SubjectRef

    repository = PostgresCaptureControlRepository(conn)
    request = SourceRequest(
        source_registry_entry_id=f"source-registry-entry:{canonical_sha256({'source': 'issue1416-e7:v1'})}",
        source_policy_id="source-policy:issue1416-e7-v1",
        request_fingerprint_version="issue1416-e7:v1",
        canonical_request_sha256=canonical_sha256({"record": record_id, "ticker": ticker}),
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


def test_a_capture_vintage_dates_bytes_that_landed_long_ago(conn, store) -> None:
    sentinels = _sentinels("V", "9000000005")
    now = datetime.now(UTC)
    _land_every_key(conn, store, sentinels, now, skip=frozenset({"financial-fact/primary/9000000005"}))
    record_id, body = "companyfacts:CIK9000000005", b'{"facts": "old"}'
    fetch_id = _land(conn, store, DataSource.SEC, record_id, body, now - timedelta(days=120))
    _use_in_a_capture(conn, fetch_id, record_id, body, sentinels[1].ticker)
    # The vintage time is the database clock; the recorder reads its recording time from it too.
    (db_now,) = conn.execute("select now()").fetchone()
    files = _record(conn, store, sentinels, recorded_at=max(db_now, now))
    facts = {e.key.describe(): e for e in _manifest(files).entries}["financial-fact/primary/9000000005"]
    assert facts.last_used_at > facts.fetched_at + timedelta(days=119)


def test_bytes_the_store_cannot_return_are_a_named_refusal(conn, store) -> None:
    sentinels = _sentinels("U", "9000000006")
    _land_every_key(conn, store, sentinels, NOW)
    store.objects.clear()
    with pytest.raises(corpus.RecordingRefused) as refused:
        _record(conn, store, sentinels)
    assert len(refused.value.findings) == len(corpus.required_keys(sentinels))
    assert all("cannot read raw.fetches:" in finding for finding in refused.value.findings)
