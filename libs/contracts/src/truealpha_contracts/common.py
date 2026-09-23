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
