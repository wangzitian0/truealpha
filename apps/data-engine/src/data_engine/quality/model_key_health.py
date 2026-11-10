# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; from dataclasses import dataclass; from typing import Any; from data_engine.sources import llm, model_policy; CALLER = "watchdog:model-key-health"; PROBE_TASK = llm.ModelTask( prompt_version="model-key-health:v1", instructions='Reply with exactly this JSON object and nothing else: {"ok": true}', response_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}, max_tokens=16, timeout_seconds=model_policy.seat_contract().probe.attempt_timeout_seconds, answer_required=False, ); AUTH_REJECTED = frozenset({401, 403})
@dataclass(frozen=True)
class KeyHealth: ok: bool; outcome: str; status_code: int | None; summary: str
def classify(error: BaseException | None, *, served_model: str | None = None) -> KeyHealth:
    if error is None: return KeyHealth(True, "answered", None, f"answered: the seated key works (served {served_model or 'model'})")
    if isinstance(error, llm.ModelNotConfigured): return KeyHealth(False, "not-configured", None, "not-configured: no model provider key is seated")
    if isinstance(error, llm.ModelCapacityRefused): return KeyHealth( False, "provider-error", None, "provider-error: no call slot or rate window before the deadline" )
    if isinstance(error, llm.ModelHTTPError):
        status = error.status_code
        if status in AUTH_REJECTED: return KeyHealth( False, "auth-rejected", status, f"auth-rejected: the provider answered HTTP {status}; the seated key is revoked or wrong", )
        code = error.provider_code; detail = f" (provider code {code})" if code is not None and code.isdigit() else ""; return KeyHealth( False, "provider-error", status, f"provider-error: the provider answered HTTP {status}{detail}" )
    if isinstance(error, OSError): return KeyHealth(False, "unreachable", None, f"unreachable: {type(error).__name__}")
    return KeyHealth(False, "provider-error", None, f"provider-error: unreadable answer ({type(error).__name__})")
def probe(*, transport: llm.Transport | None = None) -> KeyHealth:
    try: invocation = llm.invoke( None, task=PROBE_TASK, cik=0, accession="", user_content="ping", caller=CALLER, standard="watchdog", parse=_parse, refusal=lambda detail: {"ok": False}, persist=False, transport=transport, )
    except Exception as exc: return classify(exc)
    return classify(None, served_model=invocation.served_model or invocation.model)
def _parse(content: str) -> dict[str, Any]: return {"answered": bool(content)}
