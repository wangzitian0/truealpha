"""LLM-call orchestration only — the App reads Postgres directly, not through here
(init.md Section 1, rule 5).

Roadmap: the MCP endpoint comes first (reuses libs/factors, nearly free to wire into
Claude Desktop); the self-built /chat SSE endpoint is Tier 3 (Phase 7).
"""

from fastapi import FastAPI

app = FastAPI(title="truealpha-llm-service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
