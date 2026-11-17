"""Dagster op decorator binding `gateway.run_scope`."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import overload

import dagster as dg

from data_engine.sources import gateway

OpFunction = Callable[..., object]

#: The attribute that marks the body of an `external_op`, read by the registry test.
EXTERNAL_OP_MARK = "__external_op__"


def run_key_of(context: dg.OpExecutionContext) -> str:
    return f"dagster:{context.run_id}"


@overload
def external_op(function: OpFunction, /, *, name: str | None = None) -> dg.OpDefinition: ...


@overload
def external_op(function: None = None, /, *, name: str | None = None) -> Callable[[OpFunction], dg.OpDefinition]: ...


def external_op(
    function: OpFunction | None = None, /, *, name: str | None = None
) -> dg.OpDefinition | Callable[[OpFunction], dg.OpDefinition]:
    """`@dg.op` binding `gateway.run_scope` around execution."""

    def build(body: OpFunction) -> dg.OpDefinition:
        @functools.wraps(body)
        def attributed(context: dg.OpExecutionContext, *args: object, **kwargs: object) -> object:
            with gateway.run_scope(run_key_of(context)):
                return body(context, *args, **kwargs)

        setattr(attributed, EXTERNAL_OP_MARK, True)
        return dg.op(name=name)(attributed)

    return build if function is None else build(function)
