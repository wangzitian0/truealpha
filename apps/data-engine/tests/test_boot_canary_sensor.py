"""#712: a promoted data-engine build asks for one canary tick, once per image digest."""

from __future__ import annotations

import dagster as dg
import pytest
from data_engine.lanes.capture import CANARY_JOB_NAME, TICK_BY_JOB
from data_engine.lanes.triggers import boot_canary_sensor

DIGEST = "sha256:" + "7" * 64


def _evaluate(context: dg.SensorEvaluationContext) -> list:
    return list(boot_canary_sensor(context))


def test_a_build_with_a_digest_launches_the_canary_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST", DIGEST)
    monkeypatch.setenv("GIT_COMMIT_SHA", "v0.0.47")
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
    monkeypatch.setenv("TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST", other)
    (request,) = _evaluate(dg.build_sensor_context(cursor=DIGEST))
    assert isinstance(request, dg.RunRequest) and request.run_key == f"boot:{other}"


def test_without_a_digest_nothing_is_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST", raising=False)
    (skip,) = _evaluate(dg.build_sensor_context())
    assert isinstance(skip, dg.SkipReason) and "local/CI" in skip.skip_message
    monkeypatch.setenv("TRUEALPHA_DATA_ENGINE_IMAGE_DIGEST", "latest")
    (skip,) = _evaluate(dg.build_sensor_context())
    assert isinstance(skip, dg.SkipReason)
