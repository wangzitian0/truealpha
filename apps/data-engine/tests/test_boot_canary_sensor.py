"""#712: a promoted data-engine build asks for one canary tick, once per image digest.

Driven through `settings` since #784 — `TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST` and
`GIT_COMMIT_SHA` are declared in `apps/data-engine/required-env.generated.json`, so the
sensor resolves them through the model that declares them instead of reading the process
environment beside it.
"""

from __future__ import annotations

import dagster as dg
import pytest
from data_engine.lanes import triggers
from data_engine.lanes.capture import CANARY_JOB_NAME, TICK_BY_JOB
from data_engine.lanes.triggers import boot_canary_sensor

DIGEST = "sha256:" + "7" * 64


def _evaluate(context: dg.SensorEvaluationContext) -> list:
    return list(boot_canary_sensor(context))


def _build(monkeypatch: pytest.MonkeyPatch, *, digest: str, git_sha: str = "unknown") -> None:
    """The build this process is, as the deployment resolved it into `settings`."""
    monkeypatch.setattr(triggers.settings, "data_engine_image_digest", digest)
    monkeypatch.setattr(triggers.settings, "git_commit_sha", git_sha)


def test_a_build_with_a_digest_launches_the_canary_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _build(monkeypatch, digest=DIGEST, git_sha="v0.0.47")
    context = dg.build_sensor_context()
    (request,) = _evaluate(context)
    assert isinstance(request, dg.RunRequest)
    tick = TICK_BY_JOB[CANARY_JOB_NAME]
    assert request.run_key == f"boot:{DIGEST}" and request.job_name == tick.job_name
    executed_at = request.run_config["ops"][tick.op_name]["config"]["executed_at"]
    assert executed_at.endswith("+00:00")
    assert request.tags["truealpha/boot_canary"] == DIGEST and request.tags["truealpha/build"] == "v0.0.47"
    # the cursor remembers the build: the next evaluation on the same digest skips
    assert context.cursor == DIGEST
    (skip,) = _evaluate(dg.build_sensor_context(cursor=DIGEST))
    assert isinstance(skip, dg.SkipReason) and "already requested" in skip.skip_message


def test_a_new_build_on_the_same_daemon_launches_again(monkeypatch: pytest.MonkeyPatch) -> None:
    other = "sha256:" + "8" * 64
    _build(monkeypatch, digest=other)
    (request,) = _evaluate(dg.build_sensor_context(cursor=DIGEST))
    assert isinstance(request, dg.RunRequest) and request.run_key == f"boot:{other}"


def test_without_a_digest_nothing_is_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    _build(monkeypatch, digest="")
    (skip,) = _evaluate(dg.build_sensor_context())
    assert isinstance(skip, dg.SkipReason) and "local/CI" in skip.skip_message
    _build(monkeypatch, digest="latest")
    (skip,) = _evaluate(dg.build_sensor_context())
    assert isinstance(skip, dg.SkipReason)


def test_the_digest_comes_from_settings_not_from_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red before #784: the sensor read `os.environ` directly, so the value the manifest
    declares and the value the sensor acted on were two different things."""
    _build(monkeypatch, digest=DIGEST, git_sha="v0.0.47")
    monkeypatch.setenv("TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST", "sha256:" + "9" * 64)
    monkeypatch.setenv("GIT_COMMIT_SHA", "v9.9.9")
    (request,) = _evaluate(dg.build_sensor_context())
    assert request.run_key == f"boot:{DIGEST}" and request.tags["truealpha/build"] == "v0.0.47"
