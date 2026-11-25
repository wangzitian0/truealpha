"""Content addressing semantics and canonical_sha256 contract (#997)."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import BaseModel, ConfigDict, Field, model_validator
from truealpha_contracts.common import canonical_sha256, identify

pytestmark = pytest.mark.contract


class _Addressed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    thing_id: str = Field(default="", pattern=r"^(?:|thing:[0-9a-f]{64})$")
    content_sha256: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    subject: str

    @model_validator(mode="after")
    def _address(self) -> _Addressed:
        identify(self, id_field="thing_id", prefix="thing")
        return self


def test_content_addressing_semantics() -> None:
    d1, d2 = canonical_sha256({"subject": "one"}), canonical_sha256({"subject": "two"})
    m1, m2 = _Addressed(subject="one"), _Addressed(subject="two", thing_id=f"thing:{d2}", content_sha256=d2)
    assert (m1.thing_id, m1.content_sha256) == (f"thing:{d1}", d1)
    assert (m2.thing_id, m2.content_sha256) == (f"thing:{d2}", d2)
    for f, wrong in (("thing_id", "thing:" + "0" * 64), ("content_sha256", "0" * 64)):
        with pytest.raises(ValueError, match=f"{f} does not match canonical content"):
            _Addressed(subject="three", **{f: wrong})
    for bad in (None, 0, False, [], (), {"not": "a hash"}):
        m = _Addressed(subject="four")
        object.__setattr__(m, "content_sha256", bad)
        with pytest.raises(ValueError, match="content_sha256 does not match canonical content"):
            identify(m, id_field="thing_id", prefix="thing")


def test_canonical_sha256_contract() -> None:
    for sample in ({"b": 2, "a": 1}, {"nested": ["alpha", "beta"], "unicode": "café — 東京 — 🚀"}):
        raw = json.dumps(sample, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        assert canonical_sha256(sample) == hashlib.sha256(raw).hexdigest()
