"""What actually runs on the VPS, in one command — A4 D6 (#673).

Four production releases were spent fixing one redirect, and the first of them
(v0.0.26) shipped flags into a Dockerfile CMD that infra2's compose overrides —
the image CMD has never executed. The question "what is the effective
entrypoint of the deployed container" was answerable the whole time, but only
by SSH archaeology nobody performed until after the deploy.

This prints, per deployed container: image, health, restart count, started-at,
and the EFFECTIVE process line — entrypoint joined with cmd, with an explicit
marker when a compose-level entrypoint override means the image's own CMD is
dead code. Read-only; never mutates the host.

Usage:
    python tools/runtime_truth.py [name-substring]     # default filter: truealpha

Requires VPS_HOST in the environment and SSH access, same as every other
operator probe in this repository.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from typing import Any


def fetch_inspect(host: str, name_filter: str) -> list[dict[str, Any]]:
    """One SSH round-trip: list matching containers, inspect them all.

    The filter is validated against docker's own name alphabet instead of being
    escaped: this string is interpolated into a remote shell line, and a
    whitelist refuses the whole injection class rather than out-quoting it
    (review).
    """
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
        # Not assert: external data, and `python -O` strips asserts (review).
        raise SystemExit(f"runtime_truth: docker inspect returned {type(parsed).__name__}, not a list")
    return parsed


def effective_command(config: dict[str, Any]) -> tuple[str, bool]:
    """The process line the container actually runs, and whether a compose-level
    entrypoint makes the image CMD dead code.

    Two layers, stated separately because conflating them misleads (review):

    - Dockerfile layer: ENTRYPOINT and CMD compose — a non-empty entrypoint
      runs with the CMD appended as its arguments.
    - Compose layer: setting `entrypoint:` on a service ALSO clears the image's
      default CMD unless `command:` is set too (Compose spec: it "overrides the
      default entrypoint ... and clears out any default command on the image").
      The deployed containers here are the live proof — the llm image declares a
      CMD, the container's Cmd is empty.

    So a container with a compose entrypoint and empty Cmd is the v0.0.26 trap:
    everything in the image CMD is unreachable.
    """
    entrypoint = config.get("Entrypoint") or []
    cmd = config.get("Cmd") or []
    # Inline compose entrypoints are multi-line shell scripts; collapse the
    # whitespace so each container renders as one line per field.
    joined = " ".join(" ".join([*entrypoint, *cmd]).split())
    image_cmd_dead = bool(entrypoint) and not cmd
    return joined or "(none)", image_cmd_dead


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
