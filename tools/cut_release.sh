#!/usr/bin/env bash
# A4 D5 (#673): the release, as one command instead of ~7 hand steps.
#
# Encodes, as hard preconditions, the two mistakes actually made by hand in the
# 2026-08 sprint:
#   - v0.0.29 was tagged while the PR it described was still OPEN (an unresolved
#     review thread had blocked the merge and nobody checked) — so this script
#     refuses to tag unless every named PR is MERGED with zero unresolved
#     threads and its merge commit is an ancestor of main.
#   - v0.0.21/v0.0.24 were taken by a parallel agent mid-preparation — so the
#     remote tag existence check runs FIRST and the failure says "pick the next
#     number", because the tag push is the lock (docs/release-protocol.md).
#
# Usage:
#   tools/cut_release.sh vX.Y.Z --prs "663,665" --message "one-line summary" [--prod] [--dry-run]
#
# --dry-run performs every read-only assertion and prints the plan.
# Without --prod it stops after a verified staging deploy; rerun with --prod to
# promote (the staging run URL is printed for it).
set -euo pipefail

REPO="wangzitian0/truealpha"
STAGING_URL="https://truealpha-staging.truealpha.club"
PROD_URL="https://truealpha.club"

TAG="${1:?usage: cut_release.sh vX.Y.Z --prs \"N,N\" --message \"...\" [--prod] [--dry-run]}"
shift
# deploy-release.yml requires a stable vX.Y.Z tag; a malformed one would be
# pushed (the lock!) and then rejected downstream, wasting the number (review).
echo "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || { echo "cut_release: $TAG is not vX.Y.Z" >&2; exit 2; }
PRS="" MESSAGE="" PROD=0 DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prs) PRS="$2"; shift 2 ;;
    --message) MESSAGE="$2"; shift 2 ;;
    --prod) PROD=1; shift ;;
    --dry-run) DRY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$PRS" ] || { echo "--prs is required: the PRs this tag ships" >&2; exit 2; }
[ -n "$MESSAGE" ] || { echo "--message is required" >&2; exit 2; }

fail() { echo "cut_release: $*" >&2; exit 1; }
note() { echo "  $*"; }

echo "== preconditions for $TAG =="

# 1. The tag push is the lock; check the lock first so a taken number fails in
#    seconds, not after minutes of PR verification.
if git ls-remote --tags origin "refs/tags/$TAG" | grep -q .; then
  fail "$TAG already exists on origin — release identity is immutable; pick the next number"
fi
note "tag $TAG is free"

# 2. Local main must BE origin/main; tagging a stale or diverged checkout ships
#    the wrong tree under the right name. A stale checkout fast-forwards itself
#    (v0.0.35 and v0.0.38 both died on "pull first" while a parallel lane merged
#    mid-ceremony); only true divergence still fails.
git fetch origin -q
LOCAL_MAIN=$(git rev-parse main)
REMOTE_MAIN=$(git rev-parse origin/main)
if [ "$LOCAL_MAIN" != "$REMOTE_MAIN" ]; then
  git merge-base --is-ancestor "$LOCAL_MAIN" "$REMOTE_MAIN" \
    || fail "local main $LOCAL_MAIN diverged from origin/main $REMOTE_MAIN — resolve by hand"
  [ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] \
    || fail "run the ceremony from a checkout ON main (currently $(git rev-parse --abbrev-ref HEAD))"
  git merge --ff-only -q "$REMOTE_MAIN"
  LOCAL_MAIN=$(git rev-parse main)
  note "main fast-forwarded to ${LOCAL_MAIN:0:8}"
fi
note "main is current at ${LOCAL_MAIN:0:8}"

# 3. Every named PR: MERGED, zero unresolved threads, merge commit on main.
IFS=',' read -ra PR_LIST <<< "$PRS"
REVIEWED_PR=""
for PR in "${PR_LIST[@]}"; do
  PR=$(echo "$PR" | tr -d ' ')
  STATE=$(gh pr view "$PR" --repo "$REPO" --json state -q .state)
  [ "$STATE" = "MERGED" ] || fail "#$PR is $STATE, not MERGED — v0.0.29 was cut exactly this way"
  # totalCount alongside the page: with >50 threads the page could miss an
  # unresolved one, so that case fails CLOSED instead of passing by omission
  # (review).
  THREADS=$(gh api graphql -f query="{repository(owner:\"wangzitian0\",name:\"truealpha\"){pullRequest(number:$PR){reviewThreads(last:50){totalCount nodes{isResolved}}}}}")
  TOTAL=$(echo "$THREADS" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["repository"]["pullRequest"]["reviewThreads"]["totalCount"])')
  [ "$TOTAL" -le 50 ] || fail "#$PR has $TOTAL review threads, more than one page — verify by hand"
  UNRESOLVED=$(echo "$THREADS" | python3 -c 'import sys,json;n=json.load(sys.stdin)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"];print(sum(1 for x in n if not x["isResolved"]))')
  if [ "$UNRESOLVED" != "0" ]; then
    # Name them: three ceremonies in one week stalled on a bare count and the
    # operator re-ran the GraphQL by hand each time to learn WHICH threads.
    gh api graphql -f query="{repository(owner:\"wangzitian0\",name:\"truealpha\"){pullRequest(number:$PR){reviewThreads(last:50){nodes{isResolved comments(first:1){nodes{path body}}}}}}}" \
      --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved==false) | "  unresolved: " + .comments.nodes[0].path + " — " + (.comments.nodes[0].body[0:110])' >&2 || true
    fail "#$PR has $UNRESOLVED unresolved review thread(s) — listed above"
  fi
  MERGE_SHA=$(gh pr view "$PR" --repo "$REPO" --json mergeCommit -q .mergeCommit.oid)
  git merge-base --is-ancestor "$MERGE_SHA" "$LOCAL_MAIN" || fail "#$PR merge commit $MERGE_SHA is not on main"
  [ "$MERGE_SHA" = "$LOCAL_MAIN" ] && REVIEWED_PR="$PR"
  note "#$PR merged (${MERGE_SHA:0:8}), threads clear"
done
# Batch releases: deploy-release's prod gate pins the reviewed PR's
# merge_commit_sha == the release SHA, so only the PR whose merge produced
# main HEAD can be the reviewed change. v0.0.34's first prod dispatch failed
# exactly here — this script passed PR_LIST[0] (#692) while HEAD was #693's
# merge. Fail before the tag claims a version number, not at the prod gate.
[ -n "$REVIEWED_PR" ] || fail "no named PR has its merge commit at main HEAD ${LOCAL_MAIN:0:8} — include the last-merged PR (the prod gate requires reviewed merge_commit_sha == release SHA)"
note "reviewed change for prod: #$REVIEWED_PR (merge == HEAD)"

# 4. main HEAD's ci-required is green — the tag inherits this SHA. A freshly
#    merged HEAD has CI still running; wait bounded instead of failing on the
#    spot (v0.0.35's first attempt died here five minutes after its merge).
MAIN_RUN=""
for _ in $(seq 1 30); do
  STATE=$(gh run list --repo "$REPO" --workflow ci-required.yml --limit 20 \
    --json databaseId,headSha,status,conclusion \
    -q "[.[]|select(.headSha==\"$LOCAL_MAIN\")][0] | \"\(.databaseId) \(.status) \(.conclusion)\"")
  case "$STATE" in
    *"completed success") MAIN_RUN=$(echo "$STATE" | awk '{print $1}'); break ;;
    *completed*) fail "ci-required for main HEAD ${LOCAL_MAIN:0:8} finished non-green: $STATE" ;;
  esac
  sleep 30
done
[ -n "$MAIN_RUN" ] || fail "ci-required for main HEAD ${LOCAL_MAIN:0:8} not green after 15 minutes"
note "main HEAD green (run $MAIN_RUN)"

if [ "$DRY" = "1" ]; then
  echo "== dry run: would tag ${LOCAL_MAIN:0:8} as $TAG, deploy staging$([ "$PROD" = "1" ] && echo ', then prod') =="
  exit 0
fi

echo "== tagging =="
git tag -a "$TAG" "$LOCAL_MAIN" -m "$MESSAGE"
git push origin "$TAG"
note "$TAG pushed — the lock is claimed"

echo "== waiting for tag ci-required =="
TAG_RUN=""
for _ in $(seq 1 40); do
  TAG_RUN=$(gh run list --repo "$REPO" --limit 30 --json databaseId,headBranch,event,status,conclusion \
    -q "[.[]|select(.headBranch==\"$TAG\" and .event==\"push\")][0] | \"\(.databaseId) \(.status) \(.conclusion)\"")
  case "$TAG_RUN" in
    *completed\ success) break ;;
    *completed*) fail "tag ci-required failed: $TAG_RUN" ;;
  esac
  sleep 30
done
TAG_RUN_ID=$(echo "$TAG_RUN" | awk '{print $1}')
[ -n "$TAG_RUN_ID" ] || fail "tag run never appeared"
note "tag run $TAG_RUN_ID green"

deploy() { # $1=staging|prod, extra -f args after
  local TYPE="$1"; shift
  gh workflow run deploy-release.yml --repo "$REPO" \
    -f deploy_type="$TYPE" -f version_ref="$TAG" -f source_run_id="$TAG_RUN_ID" "$@" >/dev/null
  sleep 12
  local RUN
  RUN=$(gh run list --repo "$REPO" --workflow deploy-release.yml --limit 1 --json databaseId -q '.[0].databaseId')
  for _ in $(seq 1 40); do
    local S
    S=$(gh run view "$RUN" --repo "$REPO" --json status,conclusion -q '"\(.status) \(.conclusion)"')
    case "$S" in
      completed\ success) echo "$RUN"; return 0 ;;
      completed*) fail "$TYPE deploy run $RUN: $S" ;;
    esac
    sleep 30
  done
  fail "$TYPE deploy run $RUN timed out"
}

probe() { # $1=base url — the deployed identity must BE the tag (rule 6: serving, not tagged)
  local GOT
  GOT=$(curl -s --max-time 25 "$1/api/health" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("git_sha",""))' 2>/dev/null || true)
  [ "$GOT" = "$TAG" ] || fail "$1 serves ${GOT:-nothing}, expected $TAG"
  note "$1 serves $TAG"
}

echo "== staging =="
STAGING_RUN=$(deploy staging | tail -1)
probe "$STAGING_URL"
note "staging run $STAGING_RUN (walk evidence inside)"

if [ "$PROD" = "1" ]; then
  echo "== prod =="
  PROD_RUN=$(deploy prod \
    -f staging_run_url="https://github.com/$REPO/actions/runs/$STAGING_RUN" \
    -f reviewed_change_url="https://github.com/$REPO/pull/$REVIEWED_PR" | tail -1)
  probe "$PROD_URL"
  note "prod run $PROD_RUN"
else
  echo "staging verified; promote with:"
  echo "  tools/cut_release.sh $TAG --prs \"$PRS\" --message \"...\" --prod  # staging_run=$STAGING_RUN"
fi
echo "== done =="
