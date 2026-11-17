"""Tests for immutable subject and universe coordinate contracts."""

import pytest
from pydantic import ValidationError
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef

SHA = "0" * 64


def test_subject_and_universe_ref_reject_mutable_markers() -> None:
    sub = SubjectRef(kind=SubjectKind.ISSUER, id="issuer:cik:0000320193")
    uni = UniverseRef(universe_id="universe:topt-us-2026-03-31", universe_version="2026-03-31", content_sha256=SHA)
    assert sub.kind is SubjectKind.ISSUER and uni.content_sha256 == SHA
    with pytest.raises(ValidationError):
        SubjectRef(kind=SubjectKind.ISSUER, id="issuer:latest")
    with pytest.raises(ValidationError):
        UniverseRef(universe_id="universe:topt", universe_version="current", content_sha256=SHA)
