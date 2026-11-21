"""TrueAlpha LLM service."""

import os

# Each libpq connection of this process starts with JIT off (#1707). An API read returns in
# milliseconds, but Postgres JIT compiles for seconds when a plan's cost estimate drifts above
# `jit_above_cost`; the estimate moves with table statistics, so one query flipped between
# 0.02 s and 2.8 s under the 5 s statement timeout. PGOPTIONS reaches every psycopg connection,
# the truealpha_runtime readers included.
JIT_OFF_OPTION = "-c jit=off"
if JIT_OFF_OPTION not in os.environ.get("PGOPTIONS", ""):
    os.environ["PGOPTIONS"] = f"{os.environ.get('PGOPTIONS', '')} {JIT_OFF_OPTION}".strip()
