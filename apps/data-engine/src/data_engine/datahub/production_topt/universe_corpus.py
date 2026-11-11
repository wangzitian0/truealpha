"""Frozen-universe corpus loading and denominator verification."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from truealpha_contracts.capture_control import CaptureListVersion
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef

from data_engine.datahub.control_plane import frozen_topt_universe

TOPT_CORPUS_FILENAME = "corpus.v1.json"
_FROZEN_TOPT_EFFECTIVE_AT = datetime(2026, 4, 1, tzinfo=UTC)
_EXPECTED_INSTRUMENT_MAPPING_SHA256 = "e240ebf2239b94f2eb6463ad73aba89525787b52e6614b382428e8135a1a0c2e"


def load_corpus(filename: str) -> dict[str, Any]:
    from importlib import resources

    return json.loads(resources.files("data_engine.datahub.data").joinpath(filename).read_bytes())


def corpus_universe(corpus: dict[str, Any]) -> UniverseRef:
    denominator = corpus["topt_denominator"]
    return UniverseRef(
        universe_id=denominator["universe_id"],
        universe_version=f"{denominator['universe_id'].removeprefix('universe:')}-v1",
        content_sha256=denominator["instrument_mapping_sha256"],
    )


def _check_denominator(denominator: Mapping[str, Any], *, subject: str) -> None:
    instruments = denominator["instruments"]
    if int(denominator["instrument_count"]) != len(instruments):
        raise ValueError(f"{subject} instrument denominator shrink")
    if int(denominator["issuer_count"]) != len({str(row[0]) for row in instruments}):
        raise ValueError(f"{subject} issuer denominator drift")
    for column, label in ((1, "security"), (2, "listing")):
        values = [str(row[column]) for row in instruments]
        if len(values) != len(set(values)):
            raise ValueError(f"{subject} {label} denominator contains duplicates")


def _mapping_sha256(denominator: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {"fields": denominator["instrument_tuple_fields"], "instruments": denominator["instruments"]}
    )


def _build_list_version(
    denominator: Mapping[str, Any],
    *,
    subject: str,
    expected_sha: str,
    sha_error: str,
    universe: UniverseRef,
    effective_at: datetime,
) -> CaptureListVersion:
    _check_denominator(denominator, subject=subject)
    if _mapping_sha256(denominator) != expected_sha:
        raise ValueError(sha_error)
    return CaptureListVersion(
        universe=universe,
        members=tuple(SubjectRef(kind=SubjectKind.LISTING, id=str(row[2])) for row in denominator["instruments"]),
        effective_at=effective_at,
    )


def corpus_list_version(corpus: dict[str, Any]) -> CaptureListVersion:
    denominator = corpus["topt_denominator"]
    effective_at = datetime.combine(
        datetime.strptime(denominator["report_date"], "%Y-%m-%d").date(),
        datetime.min.time(),
        tzinfo=UTC,
    )
    return _build_list_version(
        denominator,
        subject="universe corpus",
        expected_sha=denominator["instrument_mapping_sha256"],
        sha_error="universe corpus instrument mapping drift",
        universe=corpus_universe(corpus),
        effective_at=effective_at,
    )


def frozen_topt_list_version(corpus: Mapping[str, Any]) -> CaptureListVersion:
    denominator = corpus["topt_denominator"]
    version = _build_list_version(
        denominator,
        subject="TOPT",
        expected_sha=_EXPECTED_INSTRUMENT_MAPPING_SHA256,
        sha_error="frozen TOPT instrument mapping drift",
        universe=frozen_topt_universe(corpus),
        effective_at=_FROZEN_TOPT_EFFECTIVE_AT,
    )
    if version.list_version_id != denominator["list_version_id"]:
        raise ValueError("frozen TOPT list identity drift")
    return version
