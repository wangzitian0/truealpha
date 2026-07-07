"""Factor registry.

Registration is how Dagster (Phase 1+) will discover assets; until then it lets
local scripts enumerate factors and their base/composite kind.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

FactorKind = Literal["base", "composite"]


@dataclass(frozen=True)
class FactorSpec:
    name: str
    kind: FactorKind
    fn: Callable
    module: int  # init.md Section 7 module number (1-7)


FACTOR_REGISTRY: dict[str, FactorSpec] = {}


def factor(name: str, *, kind: FactorKind, module: int) -> Callable:
    """Register a factor function under a stable name."""

    def decorator(fn: Callable) -> Callable:
        if name in FACTOR_REGISTRY:
            raise ValueError(f"duplicate factor name: {name}")
        FACTOR_REGISTRY[name] = FactorSpec(name=name, kind=kind, fn=fn, module=module)
        return fn

    return decorator
