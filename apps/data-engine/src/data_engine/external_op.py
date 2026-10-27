"""`external_op`: the Dagster op that can reach a vendor, attributed to its run (#1521 step 4).

Every call to a vendor or a model provider writes one row in `staging.api_call_ledger`. The row names the
Dagster run that made the call (`run_key = dagster:<run_id>`), so a run can be joined to the spend it caused.
An op that binds the run itself can forget to. `external_op` binds it for the whole op, so no op body does.

It replaces `@dg.op` for every op under `data_engine.lanes`, whether the op reaches a vendor today or not: a
later change to the body then needs no second decorator. It takes the arguments of `@dg.op` and binds only
the run. Admission against the capacity of a seat (`gateway.capacity_scope`) stays with the op: the gate is
a separate decision (#1520).

A call outside every run, for example a call from a REPL, is recorded with a NULL `run_key` and counted by the
dashboard. The gateway does not refuse it (owner decision D5).
"""

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
    """The ledger run key of a Dagster run: `dagster:<run_id>`."""
    return f"dagster:{context.run_id}"


@overload
def external_op(function: OpFunction, /, *, name: str | None = None) -> dg.OpDefinition: ...


@overload
def external_op(function: None = None, /, *, name: str | None = None) -> Callable[[OpFunction], dg.OpDefinition]: ...


def external_op(
    function: OpFunction | None = None, /, *, name: str | None = None
) -> dg.OpDefinition | Callable[[OpFunction], dg.OpDefinition]:
    """`@dg.op` with the ledger run bound around the op body. Use as `@external_op` or `@external_op(name=...)`."""

    def build(body: OpFunction) -> dg.OpDefinition:
        @functools.wraps(body)
        def attributed(context: dg.OpExecutionContext, *args: object, **kwargs: object) -> object:
            with gateway.run_scope(run_key_of(context)):
                return body(context, *args, **kwargs)

        setattr(attributed, EXTERNAL_OP_MARK, True)
        return dg.op(name=name)(attributed)

    return build if function is None else build(function)
