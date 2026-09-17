"""#712: a deployed data-engine build asks for one canary tick, once per deployment.

Driven through `settings` since #784 — `TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST`,
`TRUEALPHA_CONFIGURATION_SHA256` and `GIT_COMMIT_SHA` are declared in
`apps/data-engine/required-env.generated.json`, so the sensor resolves them through the
model that declares them instead of reading the process environment beside it.

Since 2026-09-17 (owner decision) the canary forces a fresh vendor fetch on staging only,
and a deployment is keyed by digest + configuration + an ordinal so a rolled-back digest
proves itself again (#885 item 8).
"""

from __future__ import annotations

import dagster as dg
import pytest
from data_engine.lanes import triggers
from data_engine.lanes.capture import CANARY_JOB_NAME, TICK_BY_JOB
from data_engine.lanes.triggers import boot_canary_forces_fetch, boot_canary_sensor

DIGEST = "sha256:" + "7" * 64
OTHER = "sha256:" + "8" * 64
CONFIG = "a" * 64
TICK = TICK_BY_JOB[CANARY_JOB_NAME]


def _evaluate(context: dg.SensorEvaluationContext) -> list:
    return list(boot_canary_sensor(context))


def _deploy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    digest: str,
    git_sha: str = "unknown",
    configuration: str = CONFIG,
    app_env: str = "production",
) -> None:
    """The deployment this process is, as infra2's compose resolved it into `settings`."""
    monkeypatch.setattr(triggers.settings, "data_engine_image_digest", digest)
    monkeypatch.setattr(triggers.settings, "git_commit_sha", git_sha)
    monkeypatch.setattr(triggers.settings, "configuration_sha256", configuration)
    monkeypatch.setattr(triggers.settings, "app_env", app_env)


def _launch(cursor: str | None = None) -> tuple[dg.RunRequest, str]:
    """Evaluate once and return the request plus the cursor the daemon would store."""
    context = dg.build_sensor_context(cursor=cursor)
    (request,) = _evaluate(context)
    assert isinstance(request, dg.RunRequest), request
    assert context.cursor is not None
    return request, context.cursor


def _skipped(cursor: str | None) -> dg.SkipReason:
    (skip,) = _evaluate(dg.build_sensor_context(cursor=cursor))
    assert isinstance(skip, dg.SkipReason), skip
    return skip


def _forced(request: dg.RunRequest) -> bool:
    return request.run_config["ops"][TICK.op_name]["config"]["force_fetch"]


def test_a_build_with_a_digest_launches_the_canary_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _deploy(monkeypatch, digest=DIGEST, git_sha="v0.0.47")
    request, cursor = _launch()
    assert request.run_key == f"boot:{DIGEST}:deploy-1" and request.job_name == TICK.job_name
    executed_at = request.run_config["ops"][TICK.op_name]["config"]["executed_at"]
    assert executed_at.endswith("+00:00")
    assert request.tags["truealpha/boot_canary"] == DIGEST and request.tags["truealpha/build"] == "v0.0.47"
    assert request.tags["truealpha/configuration"] == CONFIG
    # the cursor remembers the deployment: the next evaluation of it skips
    skip = _skipped(cursor)
    assert "already requested" in skip.skip_message


def test_staging_forces_a_fresh_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner decision 2026-09-17: a staging deploy proves a newly enabled source with
    real vendor bytes. Red before: the sensor never set `force_fetch`."""
    _deploy(monkeypatch, digest=DIGEST, app_env="staging")
    request, _ = _launch()
    assert _forced(request) is True


@pytest.mark.parametrize("app_env", ["production", "prod", " Production "])
def test_production_keeps_the_unforced_canary(monkeypatch: pytest.MonkeyPatch, app_env: str) -> None:
    _deploy(monkeypatch, digest=DIGEST, app_env=app_env)
    request, _ = _launch()
    assert request.run_key == f"boot:{DIGEST}:deploy-1"
    assert _forced(request) is False


@pytest.mark.parametrize("app_env", ["stg", "qa", "preview", "staging-2", ""])
def test_an_unknown_environment_does_not_force(monkeypatch: pytest.MonkeyPatch, app_env: str) -> None:
    _deploy(monkeypatch, digest=DIGEST, app_env=app_env)
    request, _ = _launch()
    assert _forced(request) is False


def test_forcing_uses_the_capture_lane_normalisation() -> None:
    """Case and surrounding whitespace are normalised as `_production_only` does; nothing
    else is guessed."""
    assert boot_canary_forces_fetch(" Staging\n") is True
    assert boot_canary_forces_fetch("staging") is True
    for other in ("production", "prod", "dev", "ci", "test", "stage", "staging_1"):
        assert boot_canary_forces_fetch(other) is False


def test_a_rolled_back_digest_proves_itself_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """#885 item 8. Red before: the key was `boot:<digest>`, which Dagster dedupes against
    every run the sensor ever launched, so redeploying DIGEST after OTHER launched nothing."""
    _deploy(monkeypatch, digest=DIGEST)
    first, cursor = _launch()
    _deploy(monkeypatch, digest=OTHER)
    second, cursor = _launch(cursor)
    _deploy(monkeypatch, digest=DIGEST)
    rollback, cursor = _launch(cursor)
    keys = [first.run_key, second.run_key, rollback.run_key]
    assert keys == [f"boot:{DIGEST}:deploy-1", f"boot:{OTHER}:deploy-2", f"boot:{DIGEST}:deploy-3"]
    assert len(set(keys)) == 3
    # the rolled-back deployment, evaluated again, is still one run
    assert "already requested" in _skipped(cursor).skip_message


def test_a_configuration_change_on_the_same_digest_is_a_new_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sources are enabled by per-environment flags (infra2 `MOOMOO_*_ORIGIN_ENABLED`),
    which change the configuration hash and not the digest. That deploy is the one that
    must prove the new source."""
    _deploy(monkeypatch, digest=DIGEST, app_env="staging")
    first, cursor = _launch()
    _deploy(monkeypatch, digest=DIGEST, configuration="b" * 64, app_env="staging")
    flipped, cursor = _launch(cursor)
    assert flipped.run_key == f"boot:{DIGEST}:deploy-2" != first.run_key
    assert _forced(flipped) is True
    assert "already requested" in _skipped(cursor).skip_message


def test_the_same_deployment_re_evaluated_yields_the_same_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the daemon launches and dies before it stores the cursor, the next evaluation
    starts from the old cursor. It must yield the SAME key, so Dagster dedupes it."""
    _deploy(monkeypatch, digest=DIGEST)
    _, cursor = _launch()
    _deploy(monkeypatch, digest=OTHER)
    retry_a, stored_a = _launch(cursor)
    retry_b, stored_b = _launch(cursor)
    assert retry_a.run_key == retry_b.run_key == f"boot:{OTHER}:deploy-2"
    assert stored_a == stored_b


def test_a_cursor_from_before_885_launches_once_more(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cursor used to be the bare digest. The first deploy of this code is a new
    build, so it launches, with a key no pre-#885 run can hold."""
    _deploy(monkeypatch, digest=OTHER)
    request, cursor = _launch(cursor=DIGEST)
    assert request.run_key == f"boot:{OTHER}:deploy-1"
    assert "already requested" in _skipped(cursor).skip_message
    # even a legacy cursor naming this very digest cannot shadow the new key format
    _deploy(monkeypatch, digest=DIGEST)
    request, _ = _launch(cursor=DIGEST)
    assert request.run_key == f"boot:{DIGEST}:deploy-1" != f"boot:{DIGEST}"


def test_without_a_digest_nothing_is_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    _deploy(monkeypatch, digest="", app_env="staging")
    assert "local/CI" in _skipped(None).skip_message
    _deploy(monkeypatch, digest="latest", app_env="staging")
    _skipped(None)


def test_identity_comes_from_settings_not_from_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red before #784: the sensor read `os.environ` directly, so the value the manifest
    declares and the value the sensor acted on were two different things."""
    _deploy(monkeypatch, digest=DIGEST, git_sha="v0.0.47", app_env="production")
    monkeypatch.setenv("TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST", "sha256:" + "9" * 64)
    monkeypatch.setenv("TRUEALPHA_CONFIGURATION_SHA256", "c" * 64)
    monkeypatch.setenv("GIT_COMMIT_SHA", "v9.9.9")
    monkeypatch.setenv("APP_ENV", "staging")
    request, _ = _launch()
    assert request.run_key == f"boot:{DIGEST}:deploy-1" and request.tags["truealpha/build"] == "v0.0.47"
    assert request.tags["truealpha/configuration"] == CONFIG
    assert _forced(request) is False


def test_the_deployed_definitions_carry_this_sensor() -> None:
    """The tests above drive the function; this pins that the composition root the daemon
    loads registers it (rule 7: assert through the deployed entry point)."""
    from data_engine.dagster_defs import defs

    assert defs.resolve_sensor_def("boot_canary_sensor") is boot_canary_sensor
