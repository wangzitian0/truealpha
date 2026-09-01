#!/usr/bin/env bash
# Pay the cold render once, before a gate opens the page — #698.
#
# v0.0.34's prod deploy: the surface walk timed out on `networkidle` at
# /research/rankings ~2 min after the container swap, and a rerun on the warm
# container passed with no code change (run 33356222058), while
# deploy-freshness had walked the identical app code green on the two
# preceding days. The health gate before the walk polls llm-service, which is a
# DIFFERENT container from app-web — nothing was warming the pages the walk
# opens, so a cold Next.js render plus a cold PG cache spent the whole 30 s
# navigation budget on the heaviest route.
#
# This VERIFIES NOTHING and can never fail: it exits 0 on every path, including
# when the surface never answers. The gate is whatever runs next. A warm-up
# that can go red would turn a slow page into a failed release — a worse
# version of the problem it exists to fix.
#
# Budget is wall-clock, not an attempt count: with a per-attempt timeout, "18
# attempts" is anywhere from a few seconds to 7 minutes, and the state this
# exists for (up but slow) is exactly the one where each attempt runs long
# (review).
#
# Usage:
#   tools/warm_surface.sh <base-url> <path> [path...]
set -uo pipefail

BUDGET_SECONDS="${WARM_BUDGET_SECONDS:-90}"
ATTEMPT_TIMEOUT="${WARM_ATTEMPT_TIMEOUT:-10}"

if [ "$#" -lt 2 ]; then
  echo "warm_surface: usage: warm_surface.sh <base-url> <path> [path...]" >&2
  exit 0   # even misuse must not fail the caller; the gate is elsewhere
fi

BASE="${1%/}"
shift
DEADLINE=$((SECONDS + BUDGET_SECONDS))

for path in "$@"; do
  while :; do
    remaining=$((DEADLINE - SECONDS))
    if [ "$remaining" -le 0 ]; then
      echo "warm: $path — ${BUDGET_SECONDS}s budget spent, letting the gate report it"
      break
    fi
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$ATTEMPT_TIMEOUT" "$BASE$path" 2>/dev/null) || code=000
    # Recomputed AFTER the request: the attempt can consume most of the budget,
    # and reporting the figure from before it tells whoever is reading a slow
    # release the wrong number in exactly the case they are reading it (review
    # on #714, which arrived after the merge).
    remaining=$((DEADLINE - SECONDS))
    case "$code" in
      2*|3*)
        echo "warm: $path answered $code (${remaining}s of budget left)"
        break
        ;;
    esac
    echo "warm: $path answered ${code:-000}, ${remaining}s of budget left"
    sleep 2
  done
done
exit 0
