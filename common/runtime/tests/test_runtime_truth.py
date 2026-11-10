"""Tests for tools/runtime_truth.py — A4 D6 (#673).

Only the pure functions: `fetch_inspect` is one SSH+docker call and is proven by
running the tool against the real VPS (recorded in the PR), not by mocking a
transport just to watch the mock. What must not regress silently is the
JUDGMENT — which container states get the v0.0.26 warning — so that is what the
canned inspect payloads pin.
"""

from __future__ import annotations

from truealpha_runtime.testing import load_tool

_module = load_tool("runtime_truth")


def _config(entrypoint: list[str] | None, cmd: list[str] | None) -> dict:
    return {"Entrypoint": entrypoint, "Cmd": cmd, "Image": "ghcr.io/x/y:v1"}


def test_a_compose_entrypoint_with_empty_cmd_is_flagged_as_the_v26_trap() -> None:
    """The exact production shape that ate a release: infra2's compose sets an
    inline shell entrypoint, Cmd is empty, so the image's own CMD never runs."""
    command, dead = _module.effective_command(_config(["sh", "-c", "exec uvicorn app"], None))
    assert dead, "an entrypoint with no Cmd means the image CMD is dead code and must be flagged"
    assert "uvicorn" in command


def test_a_plain_image_cmd_is_not_flagged() -> None:
    command, dead = _module.effective_command(_config(None, ["uvicorn", "app"]))
    assert not dead
    assert command == "uvicorn app"


def test_entrypoint_plus_cmd_is_joined_and_not_flagged() -> None:
    """Compose `entrypoint:` with `command:` keeps both; nothing is dead."""
    command, dead = _module.effective_command(_config(["tini", "--"], ["uvicorn", "app"]))
    assert not dead
    assert command == "tini -- uvicorn app"


def test_render_carries_the_warning_to_the_operator() -> None:
    containers = [
        {
            "Name": "/truealpha-llm",
            "Config": _config(["sh", "-c", "exec uvicorn app"], None),
            "State": {"Health": {"Status": "healthy"}, "StartedAt": "2026-08-28T00:00:00Z"},
            "RestartCount": 0,
        }
    ]
    output = _module.render(containers)
    assert "truealpha-llm" in output
    assert "never executes" in output, "the flag exists to be SEEN; render must surface it"
