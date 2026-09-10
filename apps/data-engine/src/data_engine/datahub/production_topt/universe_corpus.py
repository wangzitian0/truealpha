"""Generic frozen-universe corpus loading (#539 QQQ expansion).

The TOPT 20 corpus predates this module and keeps its hand-pinned loader
(`frozen_topt_list_version` below, whose expected counts and mapping sha are
literals guarded by its own tests). It lived in `medium_replay` until #795
because that is where it was first needed — which meant the deployed tick
imported a replay harness to mint one list version. Every universe after it loads
through here: the corpus file is SELF-pinned — its denominator carries the
sha256 of its own instrument mapping, computed by the builder script
(`scripts/build_universe_corpus.py`) and re-verified at load, so an edited or
truncated corpus refuses to load rather than silently shrinking a denominator
(the #543/#569 identity-anchor pattern applied to scope configuration).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from truealpha_contracts.capture_control import CaptureListVersion
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef

from data_engine.datahub.control_plane import frozen_topt_universe


def load_corpus(filename: str) -> dict[str, Any]:
    """Package-data corpus by filename; lazy so Definitions load hermetically."""
    from importlib import resources

    raw = resources.files("data_engine.datahub.data").joinpath(filename).read_bytes()
    return json.loads(raw)


def corpus_universe(corpus: dict[str, Any]) -> UniverseRef:
    denominator = corpus["topt_denominator"]
    return UniverseRef(
        universe_id=denominator["universe_id"],
        universe_version=f"{denominator['universe_id'].removeprefix('universe:')}-v1",
        content_sha256=denominator["instrument_mapping_sha256"],
    )


def corpus_list_version(corpus: dict[str, Any]) -> CaptureListVersion:
    """A self-pinned corpus becomes the content-addressed list version.

    Validation mirrors `frozen_topt_list_version` field for field, with the pins
    read from the corpus itself instead of module literals: declared counts must
    match the instrument rows, identities must not duplicate, and the mapping
    sha must reproduce — any drift refuses the load.
    """
    denominator = corpus["topt_denominator"]
    instruments = denominator["instruments"]
    if int(denominator["instrument_count"]) != len(instruments):
        raise ValueError("universe corpus instrument count drift")
    issuer_ids = {str(row[0]) for row in instruments}
    if int(denominator["issuer_count"]) != len(issuer_ids):
        raise ValueError("universe corpus issuer count drift")
    for column, label in ((1, "security"), (2, "listing")):
        values = [str(row[column]) for row in instruments]
        if len(values) != len(set(values)):
            raise ValueError(f"universe corpus {label} denominator contains duplicates")
    mapping_sha256 = canonical_sha256({"fields": denominator["instrument_tuple_fields"], "instruments": instruments})
    if mapping_sha256 != denominator["instrument_mapping_sha256"]:
        raise ValueError("universe corpus instrument mapping drift")
    return CaptureListVersion(
        universe=corpus_universe(corpus),
        members=tuple(SubjectRef(kind=SubjectKind.LISTING, id=str(row[2])) for row in instruments),
        effective_at=datetime.combine(
            datetime.strptime(denominator["report_date"], "%Y-%m-%d").date(),
            datetime.min.time(),
            tzinfo=UTC,
        ),
    )


#: The frozen TOPT list's `effective_at`. Part of the corpus's own pinned identity, not a
#: clock: `frozen_topt_list_version` asserts the minted `list_version_id` equals the one the
#: corpus carries, so a wrong value here fails loudly on the next tick rather than minting a
#: second identity for the same 21 listings. Was `medium_replay._CUTOFFS[0]` before #795,
#: where it was shared with that module's replay cutoffs by coincidence of value.
_FROZEN_TOPT_EFFECTIVE_AT = datetime(2026, 4, 1, tzinfo=UTC)
_EXPECTED_ISSUER_COUNT = 20
_EXPECTED_INSTRUMENT_COUNT = 21
_EXPECTED_INSTRUMENT_MAPPING_SHA256 = "e240ebf2239b94f2eb6463ad73aba89525787b52e6614b382428e8135a1a0c2e"


def frozen_topt_list_version(corpus: Mapping[str, Any]) -> CaptureListVersion:
    denominator = corpus["topt_denominator"]
    instruments = denominator["instruments"]
    if (
        int(denominator["instrument_count"]) != _EXPECTED_INSTRUMENT_COUNT
        or len(instruments) != _EXPECTED_INSTRUMENT_COUNT
    ):
        raise ValueError("TOPT instrument denominator shrink")
    issuer_ids = {str(row[0]) for row in instruments}
    if int(denominator["issuer_count"]) != _EXPECTED_ISSUER_COUNT or len(issuer_ids) != _EXPECTED_ISSUER_COUNT:
        raise ValueError("TOPT issuer denominator drift")
    instrument_ids = tuple(str(row[1]) for row in instruments)
    if len(instrument_ids) != len(set(instrument_ids)):
        raise ValueError("TOPT security denominator contains duplicates")
    listings = tuple(str(row[2]) for row in instruments)
    if len(listings) != len(set(listings)):
        raise ValueError("TOPT listing denominator contains duplicates")
    mapping_sha256 = canonical_sha256(
        {
            "fields": denominator["instrument_tuple_fields"],
            "instruments": instruments,
        }
    )
    if mapping_sha256 != _EXPECTED_INSTRUMENT_MAPPING_SHA256:
        raise ValueError("frozen TOPT instrument mapping drift")
    version = CaptureListVersion(
        universe=frozen_topt_universe(corpus),
        members=tuple(SubjectRef(kind=SubjectKind.LISTING, id=listing) for listing in listings),
        effective_at=_FROZEN_TOPT_EFFECTIVE_AT,
    )
    if version.list_version_id != denominator["list_version_id"]:
        raise ValueError("frozen TOPT list identity drift")
    return version
