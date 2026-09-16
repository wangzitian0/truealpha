"""Factor registry.

Registration is how Dagster (Phase 1+) will discover assets; until then it lets
local scripts enumerate factors and their base/composite kind.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from truealpha_contracts.metrics import METRICS

FactorKind = Literal["base", "composite"]


@dataclass(frozen=True)
class FactorSpec:
    name: str
    kind: FactorKind
    fn: Callable
    module: int  # init.md Section 7 module number (1-7)
    #: The registered metrics a base factor consumes, in the order it reads them (#855 B2).
    #: Declared here, once, so the evaluator that projects an issuer's records into `Fact`s
    #: derives what to project from the registry instead of keeping a key tuple per factor —
    #: the hand-maintained list that made "add a factor" an edit to the composite. Empty for
    #: a composite (it consumes other factors' results) and for a factor the strategy does
    #: not project for.
    inputs: tuple[str, ...] = ()


FACTOR_REGISTRY: dict[str, FactorSpec] = {}


def factor(name: str, *, kind: FactorKind, module: int, inputs: tuple[str, ...] = ()) -> Callable:
    """Register a factor function under a stable name.

    `inputs` are metric names from `truealpha_contracts.metrics.METRICS`; a name the registry
    does not know fails at import, where a typo is a traceback rather than an issuer that is
    silently `missing` its input on every run.
    """
    unknown = [metric for metric in inputs if metric not in METRICS]
    if unknown:
        raise ValueError(f"factor {name!r} declares inputs that are not registered metrics: {unknown}")

    def decorator(fn: Callable) -> Callable:
        if name in FACTOR_REGISTRY:
            raise ValueError(f"duplicate factor name: {name}")
        FACTOR_REGISTRY[name] = FactorSpec(name=name, kind=kind, fn=fn, module=module, inputs=tuple(inputs))
        return fn

    return decorator
