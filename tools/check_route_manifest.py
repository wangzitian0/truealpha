#!/usr/bin/env python3
"""#463: enforce tools/route_manifest.json — the route-namespace contract.

Fails (exit 1) when:
  1. an app-web route (`src/app/**/route.ts` or a top-level page segment)
     falls outside app-web's declared prefixes;
  2. an llm-service route/mount (`@app.<verb>("...")` / `app.mount("...")` in
     main.py) falls outside llm-service's declared prefixes;
  3. any two services' declared prefixes overlap (one is a prefix of another
     at a path boundary) — the #463 shadowing class.

Stdlib only: runs as `python3 tools/check_route_manifest.py` in ci-web and
ci-python without any dependency setup.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "tools" / "route_manifest.json"
#: The prefix infra2's Traefik rule strips before forwarding to llm-service.
#: Verified from behaviour, not assumed: a POST to /api/mcp/ reaches a service
#: whose app is mounted at /mcp, so the prefix is gone by the time it arrives.
ROUTED_PREFIX = "/api"
APP_DIR = REPO / "apps" / "app-web" / "src" / "app"
LLM_MAIN = REPO / "apps" / "llm-service" / "src" / "llm_service" / "main.py"


def _covered(path: str, prefixes: list[str]) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def _app_web_routes() -> list[str]:
    routes: list[str] = []
    for kind in ("route.ts", "page.tsx"):
        for found in APP_DIR.rglob(kind):
            rel = found.parent.relative_to(APP_DIR).as_posix()
            # The app root's relative path is "."; Next.js route groups
            # `(group)` do not appear in the URL.
            parts = [p for p in rel.split("/") if p != "." and not (p.startswith("(") and p.endswith(")"))]
            routes.append("/" + "/".join(parts) if parts else "/")
    return sorted(set(routes))


def _llm_routes() -> list[str]:
    source = LLM_MAIN.read_text()
    return sorted(
        set(re.findall(r'@app\.\w+\(\s*"(/[^"]*)"', source)) | set(re.findall(r'\.mount\(\s*"(/[^"]*)"', source))
    )


def main() -> int:
    manifest = json.loads(MANIFEST.read_text())["services"]
    failures: list[str] = []

    web = manifest["app-web"]
    for route in _app_web_routes():
        if route == "/" and web.get("root"):
            continue
        if not _covered(route, web["owns"]):
            failures.append(f"app-web route {route!r} is outside its declared prefixes {web['owns']}")

    llm = manifest["llm-service"]
    for route in _llm_routes():
        if not _covered(route, llm["owns"]):
            failures.append(f"llm-service route {route!r} is outside its declared prefixes {llm['owns']}")

    # The redirect/TLS property this file used to assert here has moved to
    # `apps/llm-service/tests/test_health.py`, against the app itself.
    #
    # It was asserted against the Dockerfile's CMD — which production never
    # runs. infra2's compose sets an inline entrypoint ending in
    # `exec uvicorn ... --host 0.0.0.0 --port 8000`, so the image CMD is dead
    # code. That check went green through CI, two review rounds and five red
    # cases while proving nothing about the deployed service, and staging
    # redeployed with the "fix" and answered exactly as before.
    #
    # A check on a file the runtime does not execute is worse than no check: it
    # reports the property as held.

    names = list(manifest)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            for pa in manifest[a]["owns"]:
                for pb in manifest[b]["owns"]:
                    if _covered(pa, [pb]) or _covered(pb, [pa]):
                        failures.append(
                            f"prefix overlap between {a} ({pa!r}) and {b} ({pb!r}) — the #463 shadowing class"
                        )

    for failure in failures:
        print(f"::error::route manifest: {failure}", file=sys.stderr)
    if failures:
        print(
            "declare the route in tools/route_manifest.json (and mirror it in infra2's Traefik rules) or move it",
            file=sys.stderr,
        )
        return 1
    print(f"route manifest OK: {len(_app_web_routes())} app-web routes, {len(_llm_routes())} llm-service routes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
