#!/usr/bin/env bash
# The pre-push tier, mechanised — A4 A1 (#673).
#
# Round count, not CI duration, is what makes an iteration slow: a PR costs
# ~40 min end to end, of which implementation is ~25% and CI-waits plus review
# rounds are ~60%. Every review round costs ~10-15 min (fix, push, re-CI,
# answer the thread), and across the 2026-08-28 and 08-31 sessions EVERY
# actionable finding fell into one of the six categories in
# docs/pre-push-review.md — including two found by reading that list against my
# own diff, one of which (a fail-open `mapfile < <(...)`) would have shipped a
# lane that silently ran the whole suite.
#
# So this runs the mechanical half and then PRINTS the judgement half against
# the actual diff. It is deliberately not a gate: it cannot know whether a
# `|| true` is fail-open or intentional. It puts the question in front of you
# while the diff is still cheap to change.
#
# The laptop tier is scoped by design (A4 budget table: <= 60 s, changed module
# only). Heavy suites are NAMED and REFUSED, not run — the laptop is slower
# than the runners and thermally limited, so data-engine, browser walks, docker
# and mypy-wide belong to CI.
#
# Usage:
#   tools/prepush.sh            # against origin/main
#   tools/prepush.sh <base-ref>
set -uo pipefail

# mapfile, declare -A and `;;&` are bash 4+. macOS still ships 3.2 as
# /bin/bash, where those fail with errors that name none of this — and a
# pre-push check whose own failure is cryptic will simply stop being run.
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
  echo "prepush: needs bash >= 4 (this is ${BASH_VERSION:-unknown}). macOS ships 3.2 as /bin/bash;" >&2
  echo "         install a newer one (brew install bash) or run: /opt/homebrew/bin/bash tools/prepush.sh" >&2
  exit 2
fi

BASE="${1:-origin/main}"
# Explicit: `set -e` is deliberately off here (the run() helper needs to see
# non-zero exits), so an unchecked `cd "$(git rev-parse ...)"` outside a work
# tree would cd nowhere and then diff the wrong thing (review).
if ! ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || [ -z "$ROOT" ]; then
  echo "prepush: not inside a git working tree" >&2
  exit 2
fi
cd "$ROOT"

if ! git rev-parse --verify --quiet "$BASE" >/dev/null; then
  echo "prepush: no such ref $BASE" >&2
  exit 2
fi

# Untracked files are included deliberately: a brand-new tool is invisible to
# every `git diff` until it is added, and a new file is precisely what most
# needs a lint and a syntax check. Dogfooding this script found it skipping
# ITSELF for exactly that reason.
mapfile -t CHANGED < <(
  git diff --name-only "$BASE"...HEAD
  git diff --name-only
  git diff --cached --name-only
  git ls-files --others --exclude-standard
)
# Same file reachable three ways (committed, unstaged, staged); dedupe, and
# drop deletions so the checks below never open a path that is gone.
mapfile -t CHANGED < <(printf '%s\n' "${CHANGED[@]}" | sort -u | while IFS= read -r f; do [ -n "$f" ] && [ -e "$f" ] && echo "$f"; done)

if [ "${#CHANGED[@]}" -eq 0 ]; then
  echo "prepush: nothing changed against $BASE — a scripted edit that matched nothing looks exactly like this"
  exit 1
fi

echo "== changed vs $BASE (${#CHANGED[@]} files) =="
git diff --stat "$BASE"...HEAD | tail -5
echo

FAILED=0
run() { # $1=label, rest=command
  local label="$1"; shift
  local output
  if output=$("$@" 2>&1); then
    echo "  ok    $label"
  else
    echo "  FAIL  $label"
    echo "$output" | tail -15 | sed 's/^/        /'
    FAILED=1
  fi
}

# --- mechanical: only what is instant on a laptop ----------------------------
PY=(); YML=(); SH=(); TESTS=()
for path in "${CHANGED[@]}"; do
  case "$path" in
    *.py) PY+=("$path") ;;&
    .github/workflows/*.yml|.github/workflows/*.yaml) YML+=("$path") ;;
    *.sh) SH+=("$path") ;;
  esac
done

if [ "${#PY[@]}" -gt 0 ]; then
  run "ruff format --check (${#PY[@]} files)" uv run ruff format --check "${PY[@]}"
  run "ruff check (${#PY[@]} files)" uv run ruff check "${PY[@]}"
fi
for path in "${YML[@]}"; do
  # uv's interpreter, not the system one: PyYAML is a workspace dependency and
  # a bare `python3` may not have it, which would report FAIL on a valid file
  # (review).
  run "yaml parses: $path" uv run python -c "import sys,yaml; yaml.safe_load(open(sys.argv[1]))" "$path"
done
for path in "${SH[@]}"; do
  run "bash -n: $path" bash -n "$path"
done

# --- scoped tests: the changed module only -----------------------------------
# A changed file selects its own suite. Anything whose suite is a CI-tier cost
# is named and skipped rather than run (A4: nothing heavy on the laptop).
declare -A SEEN=()
SKIPPED=()
for path in "${CHANGED[@]}"; do
  target=""
  case "$path" in
    apps/data-engine/*|apps/app-web/*)
      SKIPPED+=("$path -> ${path%%/tests/*} suite (CI tier: data-engine ~168 s sharded, web walk needs a browser)")
      continue ;;
    libs/*/tests/*|apps/llm-service/tests/*) target="$path" ;;
    tools/*.py|tools/*.sh|tools/*.json)
      base=$(basename "$path"); base="${base%.*}"
      [ -f "libs/runtime/tests/test_${base}.py" ] && target="libs/runtime/tests/test_${base}.py" ;;
    .github/workflows/*) target="libs/runtime/tests/test_ci_workflows.py" ;;
    libs/*) target="$(echo "$path" | cut -d/ -f1-3)/tests" ;;
    apps/llm-service/*) target="apps/llm-service/tests" ;;
    db/migrations/*|db/*.sql) target="libs/runtime/tests/test_migration_chain.py" ;;
  esac
  if [ -n "$target" ] && [ -e "$target" ] && [ -z "${SEEN[$target]:-}" ]; then
    SEEN[$target]=1
    TESTS+=("$target")
  fi
done

if [ "${#TESTS[@]}" -gt 0 ]; then
  echo
  echo "== scoped tests: ${TESTS[*]} =="
  # The marker breaks re-entry: libs/runtime/tests/test_prepush.py runs THIS
  # script, and this script runs that test file whenever it changed — an
  # unbounded recursion that would burn a CI job to its timeout. The test skips
  # itself when it sees the marker.
  run "pytest (scoped)" env TRUEALPHA_PREPUSH=1 uv run pytest "${TESTS[@]}" -q
fi
if [ "${#SKIPPED[@]}" -gt 0 ]; then
  echo
  echo "== not run here, by design =="
  printf '  %s\n' "${SKIPPED[@]}"
fi

# --- judgement: the half a script cannot do ----------------------------------
cat <<'CATEGORIES'

== read the diff against these six (docs/pre-push-review.md) ==
  1 fragile parsing      index/split/grep-head/regex that a rename or a flag breaks
  2 names its contract    every new failure says WHICH property broke, not KeyError
  3 stale prose           comments and docstrings that describe the old behaviour
  4 injection / quoting   values reaching a shell, a URL or SQL
  5 fail-open             || true, empty fallbacks, degraded paths that report success
  6 blast radius          permissions, credentials, what a token can reach

  Every actionable review finding in the last three sessions was one of these.
CATEGORIES

echo
if [ "$FAILED" -eq 0 ]; then
  echo "prepush: mechanical checks clean. The six above are yours."
else
  echo "prepush: mechanical checks FAILED (above)."
fi
exit "$FAILED"
