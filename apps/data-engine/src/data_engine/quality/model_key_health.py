"""Can the seated model provider still answer? — one minimal ask a day (#876 W2).

#832: the provider key was revoked in production and nothing noticed; a human reading
`staging.api_call_ledger` found the 401s. Theme purity (the only daily model caller) replays
every judgement it has already made, so on a quiet day it asks nothing and a dead key stays
invisible until the first new filing.

The probe asks the smallest question through the same client every model task uses
(`sources.llm.invoke`): the request goes through the source gateway and lands in
`staging.api_call_ledger` under `CALLER`, and nothing else is persisted (`persist=False`: no
invocation record, no replay). The verdict classifies the outcome; it never carries the key,
a fingerprint of it, the provider's error text or its host — only the class and the status.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from data_engine.sources import llm

CALLER = "watchdog:model-key-health"

#: The smallest well-formed ask: the task identity the invocation plane already understands,
#: a one-field JSON answer, and a token cap that leaves room for nothing else.
PROBE_TASK = llm.ModelTask(
    prompt_version="model-key-health:v1",
    instructions='Reply with exactly this JSON object and nothing else: {"ok": true}',
    response_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
    max_tokens=16,
)

#: The statuses that mean the provider refused the CREDENTIAL, not the request.
AUTH_REJECTED = frozenset({401, 403})


@dataclass(frozen=True)
class KeyHealth:
    ok: bool
    #: `auth-rejected`, `provider-error`, `unreachable`, `not-configured`, or `answered`.
    outcome: str
    status_code: int | None
    summary: str


def classify(error: BaseException | None, *, served_model: str | None = None) -> KeyHealth:
    """The verdict for one probe's outcome. Only the class and the status are reported."""
    if error is None:
        return KeyHealth(True, "answered", None, f"answered: the seated key works (served {served_model or 'model'})")
    if isinstance(error, llm.ModelNotConfigured):
        return KeyHealth(False, "not-configured", None, "not-configured: no model provider key is seated")
    if isinstance(error, llm.ModelHTTPError):
        status = error.status_code
        if status in AUTH_REJECTED:
            return KeyHealth(
                False,
                "auth-rejected",
                status,
                f"auth-rejected: the provider answered HTTP {status}; the seated key is revoked or wrong",
            )
        return KeyHealth(False, "provider-error", status, f"provider-error: the provider answered HTTP {status}")
    if isinstance(error, OSError):
        # URLError, timeouts and refused connections are all OSErrors; their text can name
        # the host, so only the type is kept.
        return KeyHealth(False, "unreachable", None, f"unreachable: {type(error).__name__}")
    return KeyHealth(False, "provider-error", None, f"provider-error: unreadable answer ({type(error).__name__})")


def probe(*, transport: llm.Transport | None = None) -> KeyHealth:
    """Ask once, classify, never raise. Call inside `gateway.run_scope(...)` so the ledger row
    is attributed to the run that asked."""
    try:
        invocation = llm.invoke(
            None,
            task=PROBE_TASK,
            cik=0,
            accession="",
            user_content="ping",
            caller=CALLER,
            standard="watchdog",
            parse=_parse,
            refusal=lambda detail: {"ok": False},
            persist=False,
            transport=transport,
        )
    except Exception as exc:  # noqa: BLE001 - every failure is a verdict, never a traceback
        return classify(exc)
    return classify(None, served_model=invocation.served_model or invocation.model)


def _parse(content: str) -> dict[str, Any]:
    # What the model says is irrelevant: a 2xx answer is the proof the key is accepted.
    return {"answered": bool(content)}
