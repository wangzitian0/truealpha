"""Ports implemented by runtime/storage and Postgres repositories."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from truealpha_contracts.models import RawCapture, RawIngestionEnvelope, RawObjectRef


@runtime_checkable
class RawObjectStore(Protocol):
    def store(self, capture: RawCapture) -> RawIngestionEnvelope: ...

    def get(self, ref: RawObjectRef) -> bytes: ...
