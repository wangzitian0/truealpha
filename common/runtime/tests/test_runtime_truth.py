# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; from truealpha_runtime.testing import load_tool; _module = load_tool("runtime_truth")
def _config(entrypoint: list[str] | None, cmd: list[str] | None) -> dict: return {"Entrypoint": entrypoint, "Cmd": cmd, "Image": "ghcr.io/x/y:v1"}
def test_a_compose_entrypoint_with_empty_cmd_is_flagged_as_the_v26_trap() -> None: command, dead = _module.effective_command(_config(["sh", "-c", "exec uvicorn app"], None)); assert dead, "an entrypoint with no Cmd means the image CMD is dead code and must be flagged"; assert "uvicorn" in command
def test_a_plain_image_cmd_is_not_flagged() -> None: command, dead = _module.effective_command(_config(None, ["uvicorn", "app"])); assert not dead; assert command == "uvicorn app"
def test_entrypoint_plus_cmd_is_joined_and_not_flagged() -> None: command, dead = _module.effective_command(_config(["tini", "--"], ["uvicorn", "app"])); assert not dead; assert command == "tini -- uvicorn app"
def test_render_carries_the_warning_to_the_operator() -> None: containers = [ { "Name": "/truealpha-llm", "Config": _config(["sh", "-c", "exec uvicorn app"], None), "State": {"Health": {"Status": "healthy"}, "StartedAt": "2026-08-28T00:00:00Z"}, "RestartCount": 0, } ]; output = _module.render(containers); assert "truealpha-llm" in output; assert "never executes" in output, "the flag exists to be SEEN; render must surface it"
