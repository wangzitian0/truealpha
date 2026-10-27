"""#1518: a TOPT core snapshot stored before the banking leaves reloads with the hashes that it was stored under.

The fixture `topt_core_snapshot_before_1518.json` was captured with the code of main before #1518: the
snapshot id, the content hash, the payload as `_put_snapshot` stores it, and the per-issuer factor inputs
with the member hash that `staging.topt_core_snapshot_members` stores. The four banking leaves are absent
from the payload of a bank, as they were. The models must give the same ids and hashes after the leaves
exist. No database is needed.
"""

from __future__ import annotations

import json
from pathlib import Path

from data_engine.datahub.production_topt.materialization import SnapshotMember, ToptCoreSnapshot
from factors.production_topt import TCE_SNAPSHOT_FIELDS
from truealpha_contracts.common import canonical_sha256

_FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "topt_core_snapshot_before_1518.json").read_text(
        encoding="utf-8"
    )
)


def test_the_stored_snapshot_reloads_under_its_stored_id_and_content_hash() -> None:
    # The model checks the identity: it raises when the given id or hash is not the one of the content.
    snapshot = ToptCoreSnapshot.model_validate(
        {**_FIXTURE["payload"], "snapshot_id": _FIXTURE["snapshot_id"], "content_sha256": _FIXTURE["content_sha256"]}
    )
    assert snapshot.snapshot_id == _FIXTURE["snapshot_id"]
    assert snapshot.content_sha256 == _FIXTURE["content_sha256"]
    dumped = snapshot.model_dump(mode="json", exclude={"snapshot_id", "content_sha256"})
    assert canonical_sha256(dumped) == _FIXTURE["content_sha256"]
    assert dumped == _FIXTURE["payload"], "the dump carries no key that the stored payload did not carry"


def test_the_stored_factor_inputs_keep_the_member_hashes_that_were_stored() -> None:
    snapshot = ToptCoreSnapshot.model_validate(_FIXTURE["payload"])
    rebuilt = {item.issuer_id: canonical_sha256(item.model_dump(mode="json")) for item in snapshot.factor_inputs()}
    stored = {item["factor_input"]["issuer_id"]: item["member_sha256"] for item in _FIXTURE["members"]}
    assert rebuilt == stored


def test_the_stored_snapshot_holds_a_bank_and_no_banking_leaf() -> None:
    members = _FIXTURE["payload"]["members"]
    assert sorted(member["operating_branch"] for member in members) == ["financial", "non_financial"]
    assert not any(set(TCE_SNAPSHOT_FIELDS) & set(member) for member in members)


def test_a_leaf_that_is_present_enters_the_content_hash() -> None:
    """The omission rule is not blind: a bank that carries a leaf has another snapshot id."""
    payload = json.loads(json.dumps(_FIXTURE["payload"]))
    bank = next(member for member in payload["members"] if member["operating_branch"] == "financial")
    leaf = {
        **bank["total_assets"],
        "metric": "goodwill",
        "value": "3000000",
    }
    bank["goodwill"] = leaf
    with_leaf = ToptCoreSnapshot.model_validate(payload)
    assert with_leaf.snapshot_id != _FIXTURE["snapshot_id"]
    assert isinstance(with_leaf.members[0], SnapshotMember)
