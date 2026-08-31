#!/usr/bin/env bash
# Red-prove a guard mechanically — A4-bis E1 (#673).
#
# The hand-rolled dance (cp → perl edit → run guard → cp back) was performed
# ~10 times in the 2026-08-28 session alone and failed ~1 in 3: silent no-op
# edits after formatter passes, red cases run on branches without the change
# under test, restores forgotten on early exit. Each failure cost 10-40 min of
# misdiagnosis — twice concluding "the guard is inert" about a guard that was
# fine.
#
# This bakes the three failure modes into hard steps:
#   1. the edit MUST change the file (a no-op edit aborts loudly),
#   2. the guard MUST fail and its output MUST match --expect,
#   3. the file is restored by trap on EVERY exit path, and verified restored.
#
# Usage:
#   tools/redprove.sh --file F --edit 'PERL_EXPR' --expect 'REGEX' -- CMD...
#   tools/redprove.sh --self-test
set -euo pipefail

FILE="" EDIT="" EXPECT=""
if [ "${1:-}" = "--self-test" ]; then
  # Exercises every verdict path against a scratch file and a grep guard.
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  printf 'the property holds\n' > "$TMP/subject"
  SELF="$0"

  # (a) a working guard: edit lands, guard (grep for the property) fails -> PASS
  "$SELF" --file "$TMP/subject" --edit 's/holds/broken/' --expect 'redprove' -- \
    sh -c "grep -q 'the property holds' '$TMP/subject' || { echo 'redprove: property gone'; exit 1; }" \
    >/dev/null || { echo "self-test (a) FAILED: working guard not proven"; exit 1; }
  grep -q 'the property holds' "$TMP/subject" || { echo "self-test (a) FAILED: not restored"; exit 1; }

  # (b) a no-op edit must abort
  if "$SELF" --file "$TMP/subject" --edit 's/never-matches-anything/x/' --expect 'x' -- true >/dev/null 2>&1; then
    echo "self-test (b) FAILED: no-op edit not detected"; exit 1
  fi

  # (c) an inert guard (passes despite the edit) must fail the proof
  if "$SELF" --file "$TMP/subject" --edit 's/holds/broken/' --expect 'x' -- true >/dev/null 2>&1; then
    echo "self-test (c) FAILED: inert guard reported as proven"; exit 1
  fi
  grep -q 'the property holds' "$TMP/subject" || { echo "self-test (c) FAILED: not restored"; exit 1; }

  echo "redprove self-test: all verdict paths hold"
  exit 0
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --file) FILE="$2"; shift 2 ;;
    --edit) EDIT="$2"; shift 2 ;;
    --expect) EXPECT="$2"; shift 2 ;;
    --) shift; break ;;
    *) echo "redprove: unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -n "$FILE" ] && [ -n "$EDIT" ] && [ -n "$EXPECT" ] && [ $# -gt 0 ] || {
  echo "usage: redprove.sh --file F --edit 'PERL_EXPR' --expect 'REGEX' -- CMD..." >&2; exit 2; }
[ -f "$FILE" ] || { echo "redprove: no such file $FILE" >&2; exit 2; }

BACKUP=$(mktemp)
cp "$FILE" "$BACKUP"
restore() {
  # `|| true`: traps inherit set -e, and a failing cp would abort the trap
  # before the message below ever printed.
  cp "$BACKUP" "$FILE" || true
  if cmp -s "$BACKUP" "$FILE"; then
    rm -f "$BACKUP"
  else
    # Keep the backup — it is the only remaining copy of the original, and
    # "fix by hand from $BACKUP" is a lie if this function just deleted it
    # (review on #692).
    echo "redprove: RESTORE FAILED for $FILE — fix by hand from $BACKUP" >&2
  fi
}
trap restore EXIT

perl -pi -e "$EDIT" "$FILE"
if cmp -s "$BACKUP" "$FILE"; then
  echo "redprove: the edit was a NO-OP — $FILE is byte-identical. The anchor missed" >&2
  echo "          (formatter drift?). Nothing was proven." >&2
  exit 3
fi

set +e
OUTPUT=$("$@" 2>&1)
STATUS=$?
set -e

if [ "$STATUS" -eq 0 ]; then
  echo "redprove: guard PASSED with the property broken — the guard is inert" >&2
  echo "$OUTPUT" | tail -5 >&2
  exit 4
fi
if ! echo "$OUTPUT" | grep -qE "$EXPECT"; then
  echo "redprove: guard failed but not on the expected assertion — something ELSE broke," >&2
  echo "          which proves nothing about the guard under test (the #655 Number() lesson)." >&2
  # `|| true`: this is a best-effort diagnostic — under pipefail a no-match
  # grep would kill the script HERE and replace exit 5 with exit 1 (review
  # on #692; same class as the escalate grep|head SIGPIPE).
  echo "$OUTPUT" | grep -E "Error|error|FAIL|assert" | tail -5 >&2 || true
  exit 5
fi
echo "redprove PASS: guard failed as expected —"
echo "$OUTPUT" | grep -m 2 -E "$EXPECT" | sed 's/^/  /'
