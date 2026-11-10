"""The decoding versions of the model request (#1444).

A decoding version fixes the request parameters that steer how the model thinks: the thinking
type and the reasoning effort. It is part of the invocation identity (init.md section 9), like
the prompt version. The environment contract names the version of each environment
(``model_seat.environments.<name>.decoding_version``).

- ``v1``: ``thinking`` disabled. Every answer stored before 2026-10-21 was asked this way.
- ``v2``: ``thinking`` enabled with ``reasoning_effort`` low. Z.ai documents that GLM-5.3 and
  GLM-5.3-FLASH accept only ``enabled`` (https://docs.z.ai/api-reference/llm/chat-completion and
  https://docs.z.ai/guides/llm/glm-5.3), and that ``low`` is the lightest of the three levels it
  supports (``low``, ``high``, ``max``; the default is ``max``).

``max_tokens`` is the same in every version: the task owns it. A version that thinks may need
more; the ledger shows whether an answer was cut off (``finish_reason`` length) before anyone
raises it.

A stored answer stays valid whatever its version. The replay looks for the digest of the same
question in every version of ``KNOWN`` and never asks again because an environment changed its
version. Only a version in ``KNOWN`` can be replayed, so a retired version stays listed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from data_engine.sources import model_policy


@dataclass(frozen=True)
class DecodingVersion:
    name: str
    #: The ``type`` of the ``thinking`` object in the request.
    thinking: str
    #: The ``reasoning_effort`` of the request, or None when the request carries none.
    reasoning_effort: str | None

    def parameters(self) -> dict[str, Any]:
        """The request fields this version owns. The task owns the rest."""
        fields: dict[str, Any] = {"thinking": {"type": self.thinking}}
        if self.reasoning_effort is not None:
            fields["reasoning_effort"] = self.reasoning_effort
        return fields


V1 = DecodingVersion(name="v1", thinking="disabled", reasoning_effort=None)
V2 = DecodingVersion(name="v2", thinking="enabled", reasoning_effort="low")

#: Oldest first. The first one is also the key of the lock on a question: it never changes.
KNOWN: tuple[DecodingVersion, ...] = (V1, V2)


def named(name: str) -> DecodingVersion:
    for version in KNOWN:
        if version.name == name:
            return version
    raise ValueError(f"unknown decoding version {name!r}; known: {[version.name for version in KNOWN]}")


def for_environment(environment: str) -> DecodingVersion:
    """The decoding version that the environment contract names for ``environment``."""
    return named(model_policy.seat_contract().decoding_version_for(environment))
