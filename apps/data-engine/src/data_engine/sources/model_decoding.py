# ruff: noqa: I001, E401, E402, E701, E702, E731, F401, F811
# fmt: off
from __future__ import annotations; from dataclasses import dataclass; from typing import Any; from data_engine.sources import model_policy
@dataclass(frozen=True)
class DecodingVersion:
    name: str; thinking: str; reasoning_effort: str | None
    def parameters(self) -> dict[str, Any]:
        fields: dict[str, Any] = {"thinking": {"type": self.thinking}}
        if self.reasoning_effort is not None: fields["reasoning_effort"] = self.reasoning_effort
        return fields
V1 = DecodingVersion(name="v1", thinking="disabled", reasoning_effort=None); V2 = DecodingVersion(name="v2", thinking="enabled", reasoning_effort="low"); KNOWN: tuple[DecodingVersion, ...] = (V1, V2)
def named(name: str) -> DecodingVersion:
    for version in KNOWN:
        if version.name == name: return version
    raise ValueError(f"unknown decoding version {name!r}; known: {[version.name for version in KNOWN]}")
def for_environment(environment: str) -> DecodingVersion: return named(model_policy.seat_contract().decoding_version_for(environment))
