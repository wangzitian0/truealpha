"""Tests for raw ingestion DTO contracts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from truealpha_contracts.models import DataSource, RawCapture, RawIngestionEnvelope, RawObjectRef

pytestmark = pytest.mark.contract

NOW = datetime(2026, 7, 10, tzinfo=UTC)
SHA = "a" * 64


def test_raw_object_ref_and_envelope_contracts() -> None:
    assert DataSource.NPORT.value == "nport"
    ref = RawObjectRef(
        bucket="truealpha-raw", key="sec/001.json", sha256=SHA, byte_length=42, content_type="application/json"
    )
    assert ref.uri == "s3://truealpha-raw/sec/001.json"
    cap = RawCapture(
        source=DataSource.SEC, source_record_id="acc-1", fetched_at=NOW, body=b"{}", content_type="application/json"
    )
    env = RawIngestionEnvelope(source=DataSource.SEC, source_record_id="acc-1", fetched_at=NOW, object=ref)
    assert cap.body == b"{}" and env.object == ref
    with pytest.raises(ValidationError):
        RawCapture(
            source=DataSource.SEC,
            source_record_id="acc-1",
            fetched_at=datetime(2026, 7, 10),
            body=b"{}",
            content_type="text/plain",
        )
