"""Named decoding versions for model requests (#1444)."""

from __future__ import annotations

from typing import Any

from infra2_sdk.llm import DecodingSpec

from data_engine.sources import model_policy

V1 = DecodingSpec(decoding_version="v1", thinking=False, temperature=0.0)
V2 = DecodingSpec(decoding_version="v2", thinking="low", temperature=0.0)
KNOWN: tuple[DecodingSpec, ...] = (V1, V2)


def wire_parameters(spec: DecodingSpec) -> dict[str, Any]:
    """Map SDK DecodingSpec onto Zhipu chat-completions payload fields."""
    if spec.thinking is False or spec.thinking is None:
        return {"thinking": {"type": "disabled"}}
    effort = "medium" if spec.thinking is True else str(spec.thinking)
    return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}


def named(name: str) -> DecodingSpec:
    for version in KNOWN:
        if version.decoding_version == name:
            return version
    raise ValueError(f"unknown decoding version {name!r}; known: {[version.decoding_version for version in KNOWN]}")


def for_environment(environment: str) -> DecodingSpec:
    return named(model_policy.seat_contract().decoding_version_for(environment))
