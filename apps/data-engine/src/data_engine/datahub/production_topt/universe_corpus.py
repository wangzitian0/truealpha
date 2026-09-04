"""Generic frozen-universe corpus loading (#539 QQQ expansion).

The TOPT 20 corpus predates this module and keeps its hand-pinned loader
(`medium_replay.frozen_topt_list_version`, whose expected counts and mapping
sha are literals guarded by its own tests). Every universe after it loads
through here: the corpus file is SELF-pinned — its denominator carries the
sha256 of its own instrument mapping, computed by the builder script
(`scripts/build_universe_corpus.py`) and re-verified at load, so an edited or
truncated corpus refuses to load rather than silently shrinking a denominator
(the #543/#569 identity-anchor pattern applied to scope configuration).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from truealpha_contracts.capture_control import CaptureListVersion
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.universe import SubjectKind, SubjectRef, UniverseRef


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
