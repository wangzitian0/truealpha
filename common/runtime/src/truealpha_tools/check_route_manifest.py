# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import json; import re; import sys; from pathlib import Path; REPO = Path(__file__).resolve().parents[4]; MANIFEST = REPO / "tools" / "route_manifest.json"; ROUTED_PREFIX = "/api"; APP_DIR = REPO / "apps" / "app-web" / "src" / "app"; LLM_MAIN = REPO / "apps" / "llm-service" / "src" / "llm_service" / "main.py"
def _covered(path: str, prefixes: list[str]) -> bool: return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)
def _app_web_routes() -> list[str]:
    routes: list[str] = []
    for kind in ("route.ts", "page.tsx"):
        for found in APP_DIR.rglob(kind): rel = found.parent.relative_to(APP_DIR).as_posix(); parts = [p for p in rel.split("/") if p != "." and not (p.startswith("(") and p.endswith(")"))]; routes.append("/" + "/".join(parts) if parts else "/")
    return sorted(set(routes))
def _llm_routes() -> list[str]: source = LLM_MAIN.read_text(); return sorted( set(re.findall(r'@app\.\w+\(\s*"(/[^"]*)"', source)) | set(re.findall(r'\.mount\(\s*"(/[^"]*)"', source)) )
def main() -> int:
    manifest = json.loads(MANIFEST.read_text())["services"]; failures: list[str] = []; web = manifest["app-web"]
    for route in _app_web_routes():
        if route == "/" and web.get("root"): continue
        if not _covered(route, web["owns"]): failures.append(f"app-web route {route!r} is outside its declared prefixes {web['owns']}")
    llm = manifest["llm-service"]
    for route in _llm_routes():
        if not _covered(route, llm["owns"]): failures.append(f"llm-service route {route!r} is outside its declared prefixes {llm['owns']}")
    names = list(manifest)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            for pa in manifest[a]["owns"]:
                for pb in manifest[b]["owns"]:
                    if _covered(pa, [pb]) or _covered(pb, [pa]):
                        failures.append( f"prefix overlap between {a} ({pa!r}) and {b} ({pb!r}) — the #463 shadowing class" )
    for failure in failures: print(f"::error::route manifest: {failure}", file=sys.stderr)
    if failures: print( "declare the route in tools/route_manifest.json (and mirror it in infra2's Traefik rules) or move it", file=sys.stderr, ); return 1
    print(f"route manifest OK: {len(_app_web_routes())} app-web routes, {len(_llm_routes())} llm-service routes"); return 0
if __name__ == "__main__": raise SystemExit(main())
