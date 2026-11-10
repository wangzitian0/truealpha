"""The rules that decide whether the answer of a fetch may be kept, registered once (#1521 step 3).

A vendor can answer HTTP 200 with a body that is not the resource: an HTML page for a JSON document, an
error object, a window without the row of its session. The rule of a seat and an endpoint family reads the
key and the body and says whether the body is the resource. `ACCEPT_RULES` holds each rule once, under
`(seat, endpoint family)`. No caller passes a rule, so a call site cannot leave one out.

The gateway judges an answer in two places, with the same rule. `fetch_once` lands the answer only when
the rule accepts it. The ledger row of the call carries the reason `answer_refused` when the rule refuses
it, and keeps its fetch key.

The endpoint family is the endpoint of the key up to its first `?` or `:`. A key that is a window of one
endpoint (`chart?interval=1d`) and a key that names a part of it (`financial_statements:income`) share the
rule of their family.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from data_engine.datahub.fetch_key import FetchKey

log = logging.getLogger(__name__)

Rule = Callable[[FetchKey, bytes], bool]

#: The one registry. A module registers its rules when it is imported, with `accept_rule`.
ACCEPT_RULES: dict[tuple[str, str], Rule] = {}


def endpoint_family(endpoint: str) -> str:
    """The endpoint of a key without its parameters (`chart?interval=1d` is `chart`)."""
    return re.split(r"[?:]", endpoint, maxsplit=1)[0]


def accept_rule(seat: str, family: str) -> Callable[[Rule], Rule]:
    """Register the rule of one seat and endpoint family. A second rule for the same pair is an error."""

    def register(rule: Rule) -> Rule:
        if (seat, family) in ACCEPT_RULES:
            raise ValueError(f"the accept rule of {seat}/{family} is already registered")
        ACCEPT_RULES[(seat, family)] = rule
        return rule

    return register


def accepts(key: FetchKey, body: bytes) -> bool:
    """Whether ``body`` is the resource that ``key`` names. A key with no registered rule accepts any body.

    A rule that raises on an answer it cannot read refuses the answer: it is not a final one."""
    rule = ACCEPT_RULES.get((key.seat, endpoint_family(key.endpoint)))
    if rule is None:
        return True
    try:
        return bool(rule(key, body))
    except Exception:  # noqa: BLE001 - an unreadable answer is not a final one
        log.exception("the accept rule of %s/%s raised; the answer is refused", key.seat, endpoint_family(key.endpoint))
        return False
