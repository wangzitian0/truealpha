"""Inspect effective container commands on the VPS over SSH (#673)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from typing import Any


def fetch_inspect(host: str, name_filter: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name_filter):
        raise SystemExit(f"runtime_truth: refusing name filter {name_filter!r} — [A-Za-z0-9_.-] only")
    script = (
        f"names=$(docker ps -a --filter name={name_filter} --format '{{{{.Names}}}}'); "
        f'[ -n "$names" ] && docker inspect $names || echo "[]"'
    )
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", f"root@{host}", script],
        capture_output=True,
        text=True,
        check=True,
    )
    parsed = json.loads(result.stdout)
    if not isinstance(parsed, list):
        raise SystemExit(f"runtime_truth: docker inspect returned {type(parsed).__name__}, not a list")
    return parsed


def effective_command(config: dict[str, Any]) -> tuple[str, bool]:
    entrypoint = config.get("Entrypoint") or []
    cmd = config.get("Cmd") or []
    joined = " ".join(" ".join([*entrypoint, *cmd]).split())
    return joined or "(none)", bool(entrypoint) and not cmd


def render(containers: Sequence[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in sorted(containers, key=lambda c: str(c.get("Name", ""))):
        name = str(item.get("Name", "?")).lstrip("/")
        config = item.get("Config") or {}
        state = item.get("State") or {}
        health = (state.get("Health") or {}).get("Status", "none")
        command, image_cmd_dead = effective_command(config)
        lines.append(
            f"{name}\n"
            f"  image     {config.get('Image', '?')}\n"
            f"  health    {health}  restarts={item.get('RestartCount', '?')}  "
            f"started={state.get('StartedAt', '?')}\n"
            f"  runs      {command[:160]}"
        )
        if image_cmd_dead:
            lines.append(
                "  warning   entrypoint is set and Cmd is empty — the image's own CMD "
                "never executes (the v0.0.26 trap: a fix shipped there changes nothing)"
            )
    return "\n".join(lines) if lines else "no matching containers"


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    name_filter = arguments[0] if arguments else "truealpha"
    host = os.environ.get("VPS_HOST", "")
    if not host:
        print("runtime_truth: VPS_HOST is not set", file=sys.stderr)
        return 2
    print(render(fetch_inspect(host, name_filter)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
