from datetime import UTC, datetime

from truealpha_contracts.models import DataSource, RawCapture, RawObjectRef

NOW = datetime(2026, 7, 10, tzinfo=UTC)


def test_source_values_are_stable_wire_names():
    assert DataSource.NPORT.value == "nport"


def test_raw_capture_and_object_ref_preserve_metadata():
    capture = RawCapture(
        source=DataSource.SEC, source_record_id="rec:1", fetched_at=NOW, body=b"ok", content_type="text/plain"
    )
    ref = RawObjectRef(bucket="raw", key="k/1", sha256="a" * 64, byte_length=2, content_type="text/plain")
    assert capture.source is DataSource.SEC
    assert ref.uri == "s3://raw/k/1"
