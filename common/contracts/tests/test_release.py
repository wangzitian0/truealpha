from __future__ import annotations

import re

import pytest
from truealpha_contracts.common import canonical_sha256
from truealpha_contracts.release import live_release_manifest_id, live_release_payload

_MIGRATIONS = ("0001_schemas", "20260908T1055_factors_question_coverage_report")
_CONTRACT = "e" * 64


def _live_id(**overrides: object) -> str:
    arguments: dict[str, object] = {
        "git_commit_sha": "v0.0.49",
        "migration_ids": _MIGRATIONS,
        "environment_contract_sha256": _CONTRACT,
    }
    arguments.update(overrides)
    return live_release_manifest_id(**arguments)  # type: ignore[arg-type]


def test_two_releases_do_not_share_one_identity() -> None:
    assert _live_id(git_commit_sha="v0.0.49") != _live_id(git_commit_sha="v0.0.50")
    assert _live_id(migration_ids=(*_MIGRATIONS, "20260909T0000_datahub_next")) != _live_id()
    assert _live_id(environment_contract_sha256="f" * 64) != _live_id()


def test_the_identity_is_stable_for_the_same_inputs() -> None:
    assert _live_id() == _live_id()
    assert _live_id(migration_ids=tuple(reversed(_MIGRATIONS))) != _live_id()


def test_the_identity_keeps_the_content_addressed_shape_infra2_asserts() -> None:
    assert re.fullmatch(r"^release-manifest:[0-9a-f]{64}$", _live_id())


def test_the_payload_says_what_the_identity_measured() -> None:
    payload = live_release_payload(
        git_commit_sha="v0.0.49",
        migration_ids=_MIGRATIONS,
        environment_contract_sha256=_CONTRACT,
    )
    assert payload == {
        "kind": "production-topt-live-release",
        "git_commit_sha": "v0.0.49",
        "migration_ids": list(_MIGRATIONS),
        "environment_contract_sha256": _CONTRACT,
    }
    assert live_release_manifest_id(
        git_commit_sha="v0.0.49",
        migration_ids=_MIGRATIONS,
        environment_contract_sha256=_CONTRACT,
    ) == "release-manifest:" + canonical_sha256(payload)


def test_an_unidentified_build_is_named_unknown_rather_than_refused() -> None:
    assert _live_id(git_commit_sha="unknown") not in {_live_id(), _live_id(git_commit_sha="v0.0.50")}


@pytest.mark.parametrize(
    "overrides",
    [
        {"git_commit_sha": ""},
        {"git_commit_sha": "  "},
        {"git_commit_sha": "-v0.0.49"},
        {"git_commit_sha": "v 0.0.49"},
        {"migration_ids": ()},
        {"migration_ids": ("0001_schemas", "0001_schemas")},
        {"migration_ids": ("0001_schemas", "")},
        {"environment_contract_sha256": "not-a-hash"},
        {"environment_contract_sha256": ("E" * 64)},
    ],
)
def test_an_unmeasurable_input_is_refused_rather_than_hashed(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _live_id(**overrides)


def test_surrounding_whitespace_is_the_same_release_not_a_second_one() -> None:
    assert _live_id(git_commit_sha=" v0.0.49\n") == _live_id(git_commit_sha="v0.0.49")
