"""Local, file-based stand-in for the moomoo call-budget gate (init.md Section 1
rule 6: every moomoo call must go through the gate, no module decides for
itself). This is a Phase -1 tool — real ingestion should read/write
`staging.api_call_ledger` in Postgres instead once that pipeline exists; this
file only prevents a manual audit session from blowing the monthly quota.

State lives in apps/data-engine/data/moomoo_ledger.json (gitignored).
"""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from data_engine.config import settings

LEDGER_PATH = Path(__file__).resolve().parents[3] / "data" / "moomoo_ledger.json"


class BudgetExceededError(Exception):
    pass


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _load() -> dict:
    if not LEDGER_PATH.exists():
        return {"calls": []}
    return json.loads(LEDGER_PATH.read_text())


def _save(state: dict) -> None:
    """Atomic write (temp file + os.replace): a crash or interrupt mid-write must
    never leave a corrupted ledger, since this is a hard real-world quota (2,000
    calls/month, unrecoverable once spent) — a broken ledger file must not be
    able to silently disable the gate that protects it."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=LEDGER_PATH.parent, prefix=".moomoo_ledger.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp_path, LEDGER_PATH)
    except BaseException:
        os.unlink(tmp_path)
        raise


def calls_this_month() -> int:
    state = _load()
    key = _month_key(datetime.now(UTC))
    return sum(1 for c in state["calls"] if c["month"] == key)


def record(endpoint: str, caller: str, ok: bool) -> None:
    """Record a call AFTER it happens — gate() below is what enforces the cap."""
    state = _load()
    now = datetime.now(UTC)
    state["calls"].append(
        {
            "month": _month_key(now),
            "endpoint": endpoint,
            "caller": caller,
            "called_at": now.isoformat(),
            "ok": ok,
        }
    )
    _save(state)


def gate(endpoint: str, caller: str):
    """Raise BudgetExceededError BEFORE spending a call if this month's cap is hit."""
    used = calls_this_month()
    if used >= settings.moomoo_monthly_call_budget:
        raise BudgetExceededError(
            f"moomoo monthly budget exhausted: {used}/{settings.moomoo_monthly_call_budget} "
            f"calls already made this month (blocked call: {caller} -> {endpoint})"
        )
