"""Factor registry."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from truealpha_contracts.metrics import METRICS

FactorKind = Literal["base", "composite", "feeder"]


@dataclass(frozen=True)
class FactorSpec:
    name: str
    kind: FactorKind
    fn: Callable
    module: int
    inputs: tuple[str, ...] = ()


FACTOR_REGISTRY: dict[str, FactorSpec] = {}


def factor(name: str, *, kind: FactorKind, module: int, inputs: tuple[str, ...] = ()) -> Callable:
    unknown = [metric for metric in inputs if metric not in METRICS]
    if unknown:
        raise ValueError(f"factor {name!r} declares inputs that are not registered metrics: {unknown}")

    def decorator(fn: Callable) -> Callable:
        if name in FACTOR_REGISTRY:
            raise ValueError(f"duplicate factor name: {name}")
        FACTOR_REGISTRY[name] = FactorSpec(name=name, kind=kind, fn=fn, module=module, inputs=tuple(inputs))
        return fn

    return decorator
