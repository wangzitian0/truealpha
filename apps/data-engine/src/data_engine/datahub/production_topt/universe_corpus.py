"""Generic frozen-universe corpus loading (#539 QQQ expansion).

The TOPT 20 corpus predates this module and keeps its hand-pinned loader
(`frozen_topt_list_version` below, whose mapping sha is a literal pin). It lived in
`medium_replay` until #795 because that is where it was first needed — which meant the
deployed tick imported a replay harness to mint one list version. Every universe after
it loads through here: the corpus file is SELF-pinned — its denominator carries the
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


def _check_denominator(denominator: Mapping[str, Any], *, subject: str) -> None:
    """The counts and identities a denominator declares must match its instrument rows.

    Both loaders run this one check. Each then compares `_mapping_sha256` with its own pin.
    """
    instruments = denominator["instruments"]
    if int(denominator["instrument_count"]) != len(instruments):
        raise ValueError(f"{subject} instrument denominator shrink")
    issuer_ids = {str(row[0]) for row in instruments}
    if int(denominator["issuer_count"]) != len(issuer_ids):
        raise ValueError(f"{subject} issuer denominator drift")
    for column, label in ((1, "security"), (2, "listing")):
        values = [str(row[column]) for row in instruments]
        if len(values) != len(set(values)):
            raise ValueError(f"{subject} {label} denominator contains duplicates")


def _mapping_sha256(denominator: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {"fields": denominator["instrument_tuple_fields"], "instruments": denominator["instruments"]}
    )


def corpus_list_version(corpus: dict[str, Any]) -> CaptureListVersion:
    """A self-pinned corpus becomes the content-addressed list version.

    The shared denominator check runs first. The pin is read from the corpus itself instead
    of a module literal: the mapping sha must reproduce, and any drift refuses the load.
    """
    denominator = corpus["topt_denominator"]
    instruments = denominator["instruments"]
    _check_denominator(denominator, subject="universe corpus")
    if _mapping_sha256(denominator) != denominator["instrument_mapping_sha256"]:
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
_EXPECTED_INSTRUMENT_MAPPING_SHA256 = "e240ebf2239b94f2eb6463ad73aba89525787b52e6614b382428e8135a1a0c2e"


def frozen_topt_list_version(corpus: Mapping[str, Any]) -> CaptureListVersion:
    """The frozen TOPT list. The mapping sha pin fixes the 21 listings and 20 issuers."""
    denominator = corpus["topt_denominator"]
    _check_denominator(denominator, subject="TOPT")
    if _mapping_sha256(denominator) != _EXPECTED_INSTRUMENT_MAPPING_SHA256:
        raise ValueError("frozen TOPT instrument mapping drift")
    listings = tuple(str(row[2]) for row in denominator["instruments"])
    version = CaptureListVersion(
        universe=frozen_topt_universe(corpus),
        members=tuple(SubjectRef(kind=SubjectKind.LISTING, id=listing) for listing in listings),
        effective_at=_FROZEN_TOPT_EFFECTIVE_AT,
    )
    if version.list_version_id != denominator["list_version_id"]:
        raise ValueError("frozen TOPT list identity drift")
    return version
