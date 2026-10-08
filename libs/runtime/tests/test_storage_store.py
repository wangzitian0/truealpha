from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError
from truealpha_contracts.models import DataSource, RawCapture
from truealpha_runtime import RuntimeSettings, S3RawObjectStore
from truealpha_runtime.storage import StorageError

BODY = b'{"entityName":"Datadog, Inc."}'


class HeadScriptedS3:
    """Fake S3 client whose `head_object` returns or raises a scripted outcome."""

    def __init__(self, head: dict[str, Any] | Exception) -> None:
        self.head = head
        self.put_calls: list[dict[str, Any]] = []

    def head_bucket(self, *, Bucket: str) -> dict[str, Any]:
        return {}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if isinstance(self.head, Exception):
            raise self.head
        return self.head

    def put_object(self, **kwargs: Any) -> None:
        self.put_calls.append(kwargs)


def _capture() -> RawCapture:
    return RawCapture(
        source=DataSource.SEC,
        source_record_id="0001564590-20-006422",
        body=BODY,
        content_type="application/json",
        fetched_at=datetime(2026, 7, 10, tzinfo=UTC),
    )


def _store(client: HeadScriptedS3) -> S3RawObjectStore:
    return S3RawObjectStore(RuntimeSettings(_env_file=None, app_env="test"), client=client)


def test_store_refuses_a_content_length_mismatch_with_the_collision_message() -> None:
    client = HeadScriptedS3({"ContentLength": len(BODY) + 1})

    with pytest.raises(StorageError, match="content-address collision"):
        _store(client).store(_capture())

    assert client.put_calls == []


def test_store_writes_the_object_once_when_head_reports_not_found() -> None:
    client = HeadScriptedS3(ClientError({"Error": {"Code": "404"}}, "HeadObject"))

    envelope = _store(client).store(_capture())

    assert len(client.put_calls) == 1
    assert client.put_calls[0]["Key"] == envelope.object.key
    assert client.put_calls[0]["Body"] == BODY


def test_store_reports_cannot_inspect_when_head_fails_for_another_reason() -> None:
    cause = ClientError({"Error": {"Code": "500"}}, "HeadObject")
    client = HeadScriptedS3(cause)

    with pytest.raises(StorageError, match="cannot inspect") as raised:
        _store(client).store(_capture())

    assert raised.value.__cause__ is cause
    assert client.put_calls == []


def test_store_skips_the_write_when_head_reports_the_same_length() -> None:
    client = HeadScriptedS3({"ContentLength": len(BODY)})

    envelope = _store(client).store(_capture())

    assert client.put_calls == []
    assert envelope.object.byte_length == len(BODY)
    assert envelope.object.key.endswith(envelope.object.sha256)
