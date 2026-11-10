# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; import logging; import re; from collections.abc import Callable; from data_engine.datahub.fetch_key import FetchKey; log = logging.getLogger(__name__); Rule = Callable[[FetchKey, bytes], bool]; ACCEPT_RULES: dict[tuple[str, str], Rule] = {}
def endpoint_family(endpoint: str) -> str: return re.split(r"[?:]", endpoint, maxsplit=1)[0]
def accept_rule(seat: str, family: str) -> Callable[[Rule], Rule]:
    def register(rule: Rule) -> Rule:
        if (seat, family) in ACCEPT_RULES: raise ValueError(f"the accept rule of {seat}/{family} is already registered")
        ACCEPT_RULES[(seat, family)] = rule; return rule
    return register
def accepts(key: FetchKey, body: bytes) -> bool:
    rule = ACCEPT_RULES.get((key.seat, endpoint_family(key.endpoint)))
    if rule is None: return True
    try: return bool(rule(key, body))
    except Exception: log.exception("the accept rule of %s/%s raised; the answer is refused", key.seat, endpoint_family(key.endpoint)); return False
