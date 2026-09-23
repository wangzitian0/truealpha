"""Dependency-neutral primitives shared by immutable contract modules."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def identify(model: BaseModel, *, id_field: str, prefix: str, hash_field: str = "content_sha256") -> None:
    """Stamp a frozen model with its content address, refusing one that disagrees.

    The hash covers the model's JSON payload with the two identity fields removed, so it is
    a function of the content alone; the id is that hash under a namespace prefix. A model
    that supplies neither is stamped. One that supplies either must supply the value the
    content produces.

    #997: nine copies of this wrapper lived across the contract modules -- two of them under
    the name `_content_address`, which is why no search for `_identify` found them -- while
    `canonical_sha256` right above had been shared from the start. Seven accepted a supplied
    value that was falsy-but-not-empty and two refused it, so the same object was valid or
    invalid depending on which module defined it.

    This keeps the stricter reading. "Not supplied" is the empty string, the declared default
    of every identity field in these contracts; anything else must equal what the content
    produces. The two checks stay separate, as seven of the nine had them, because "the hash
    disagrees" and "the id disagrees" are different faults and the caller should be told
    which. Membership is tested against a tuple rather than a set so an unhashable supplied
    value is refused rather than raising TypeError from the check itself.
    """
    payload = model.model_dump(mode="json", exclude={id_field, hash_field})
    expected_hash = canonical_sha256(payload)
    expected_id = f"{prefix}:{expected_hash}"
    supplied_hash = getattr(model, hash_field)
    supplied_id = getattr(model, id_field)
    if supplied_hash not in ("", expected_hash):
        raise ValueError(f"{hash_field} does not match canonical content")
    if supplied_id not in ("", expected_id):
        raise ValueError(f"{id_field} does not match canonical content")
    object.__setattr__(model, hash_field, expected_hash)
    object.__setattr__(model, id_field, expected_id)


class CaptureEnvironment(StrEnum):
    # `local` remains only for the pre-Gate-0 bounded adapter. Frozen contracts
    # use the explicit logical tiers below.
    LOCAL = "local"
    LOCAL_DEV = "local_dev"
    LOCAL_TEST = "local_test"
    GITHUB_CI = "github_ci"
    PREVIEW = "preview"
    STAGING = "staging"
    PRODUCTION = "production"


def identify_by_grain(model: BaseModel, *, id_field: str, prefix: str, identity_fields: tuple[str, ...]) -> None:
    """Stamp a model whose id is a NATURAL KEY and whose hash is its whole content.

    The second identity shape in these contracts, and a different contract from `identify`:
    the id hashes only the fields named in `identity_fields`, under a `{"kind", "identity"}`
    envelope, so two records with the same key are the same record however their other fields
    move; the hash still covers everything, so a restatement is distinguishable.

    #997: `datahub._freeze_identity` and `reconciliation._freeze_content` were this function
    twice, differing in two error-message phrasings. The derivation was identical -- same
    envelope, same content payload -- so sharing it changes no id already minted.

    `capture_control._freeze_wrapped` was a third copy, merged on the #1009 review: identical
    derivation, and its set-membership guard is this one's tuple guard for every hashable
    value. `capture_control._freeze` is NOT this function -- it hashes the identity without the
    envelope, so it mints different ids, and merging it is its own change with its own
    equivalence argument.
    """
    identity = model.model_dump(mode="json", include=set(identity_fields))
    expected_id = f"{prefix}:{canonical_sha256({'kind': prefix, 'identity': identity})}"
    expected_hash = canonical_sha256(model.model_dump(mode="json", exclude={id_field, "content_sha256"}))
    supplied_id = getattr(model, id_field)
    supplied_hash = getattr(model, "content_sha256")
    if supplied_id not in ("", expected_id):
        raise ValueError(f"{id_field} does not match its declared identity grain")
    if supplied_hash not in ("", expected_hash):
        raise ValueError("content_sha256 does not match the canonical record")
    object.__setattr__(model, id_field, expected_id)
    object.__setattr__(model, "content_sha256", expected_hash)
