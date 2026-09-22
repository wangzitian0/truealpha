#!/usr/bin/env bash
# A4 D5 (#673): the release, as one command instead of ~7 hand steps.
#
# Encodes, as hard preconditions, mistakes actually made by hand:
#   - v0.0.29 was tagged while the PR it described was still OPEN (an unresolved
#     review thread had blocked the merge and nobody checked) — so this script
#     refuses to tag unless every named PR is MERGED with zero unresolved
#     threads and its merge commit is an ancestor of main.
#   - v0.0.21/v0.0.24 were taken by a parallel agent mid-preparation — so the
#     remote tag existence check runs FIRST and the failure says "pick the next
#     number", because the tag push is the lock (docs/release-protocol.md).
#   - v0.0.60 (2026-09-15) died after the tag push twice — a killed process and
#     a flaky post-deploy walk — and re-running the plain command failed at the
#     tag-collision check even though the right move was to keep watching what
#     was already in flight (#811). `--resume` treats an existing tag as this
#     release already underway IF it points at local main's HEAD (never
#     otherwise — the tag push stays the lock); `--redeploy` re-runs just the
#     deploy leg once the tag and its CI are already green.
#   - v0.0.63/66/67 (2026-09-16, #860) collided with another lane THREE times
#     in one day: step 1's ls-remote check is a fast fail, not the lock, and
#     minutes can pass between it and the actual `git push` (PR verification,
#     the main-HEAD CI wait) — long enough for another release to land in
#     between. The push itself re-checks immediately before claiming the
#     number and rolls forward to the next free vX.Y.(Z+1) on a genuine race,
#     instead of failing the whole ceremony after all that verification work
#     (see `push_tag_with_last_instant_lock` below). Step 1 stays: it is still
#     the fast, cheap fail for a number that is obviously already taken.
#
# Usage:
#   tools/cut_release.sh vX.Y.Z --message "one-line summary" \
#     [--prs "663,665"] [--prod] [--dry-run] [--resume] [--redeploy] [--auto]
#
# --auto marks the tag as machine-cut (`auto-release-staging.yml`, #860): it
#   appends `Release-Trigger: auto-staging` to the tag annotation, which is the
#   only thing `tools/auto_release.py`'s daily cap counts (a hand-cut tag never
#   carries it, so an operator release never eats into the automatic budget).
#   It is mutually exclusive with --prod and checked FIRST, before the tag
#   regex or anything else that touches git or gh — the owner's 2026-09-17
#   decision ("先在 staging 做吧，prod 回头再说", #860) is that an automatic
#   release is staging-only, full stop, so this combination refuses before any
#   side effect rather than after deriving --prs or verifying a PR. It also
#   dispatches the staging surface walk explicitly once the staging deploy is
#   green (#945): the deploy dispatch above runs under github.token, which
#   GitHub never cascades into a workflow_run trigger, so relying on
#   deploy-release.yml's completion to fire walk-release.yml (as the non-auto
#   path does, under a real operator PAT) would wait forever.
#
# --prs is now OPTIONAL (#855 A3, #860): omitted, the PR list is DERIVED from
#   every squash-merge between the newest reachable vX.Y.Z tag and main HEAD
#   (`git log <tag>..main --format=%s`, trailing `(#N)`), printed for review,
#   and recorded in the tag's own annotation. This is the cadence change: one
#   release per batch of merged PRs, not one release per PR — v0.0.56-v0.0.60
#   on 2026-09-15 were five tags for five PRs, each paying the full ~10 min
#   tag-CI + deploy pipeline alone. Passing --prs explicitly still works
#   exactly as before (a break-glass single-PR release stays possible) and is
#   REQUIRED when no prior release tag exists to derive a range from. With
#   --resume/--redeploy the range starts at the release BEFORE $TAG, since
#   $TAG itself already sits at main HEAD (#913).
# --dry-run performs every read-only assertion and prints the plan.
# --resume: $TAG already exists on origin (script was killed, laptop slept, the
#   post-deploy walk flaked after the tag was pushed) — verify the tag is at
#   local main's HEAD instead of failing the collision check, skip re-tagging,
#   and continue from the tag ci-required wait (already green, it falls
#   straight through).
# --redeploy: the tag and its ci-required are already known green (#811 — only
#   the deploy run's gate flaked); skip tagging and the tag-CI wait entirely
#   and go straight to dispatching the staging deploy.
# Without --prod it stops after staging is verified TWO ways — the deploy run
# green AND a green surface walk for this tag (#855/#860: the walk is now a
# separate, deferred workflow, so "the deploy succeeded" no longer implies "and
# it was walked") — then prints the --prod command. A walk failure prints the
# one-line re-run instead of re-dispatching the whole deploy:
#   gh workflow run walk-release.yml -f deploy_type=staging -f version_ref=$TAG
#
# Promotion policy (#819) — deploy-freshness.yml and tools/deploy_freshness.py
# bound the same policy, so the three files must agree:
#   - every tag soaks staging: the staging deploy below is unconditional;
#   - prod moves only with --prod: the owner promotes deliberately, so prod
#     lags staging by design and several tags can soak before one is promoted;
#   - the daily freshness check bounds that lag at staging 3 days and
#     production 14 days. Past the bound the leg is red and files an issue
#     (#680); the answer is a --prod run, not a wider bound.
set -euo pipefail

REPO="wangzitian0/truealpha"
STAGING_URL="https://truealpha-staging.truealpha.club"
PROD_URL="https://truealpha.club"

TAG="${1:?usage: cut_release.sh vX.Y.Z --message \"...\" [--prs \"N,N\"] [--prod] [--dry-run] [--resume] [--redeploy] [--auto]}"
shift
PRS="" MESSAGE="" PROD=0 DRY=0 RESUME=0 REDEPLOY=0 AUTO=0
# Set once THIS run pushes a brand-new tag (not a --resume of one already on
# origin) — see abandon_fresh_tag_on_origin below.
FRESHLY_TAGGED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prs) PRS="$2"; shift 2 ;;
    --message) MESSAGE="$2"; shift 2 ;;
    --prod) PROD=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --resume) RESUME=1; shift ;;
    --redeploy) REDEPLOY=1; shift ;;
    --auto) AUTO=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
# Checked before the tag regex, before --prs derivation, before anything else
# touches git or gh: --auto is how auto-release-staging.yml (#860) cuts a
# release, and the owner's 2026-09-17 decision is staging-only, full stop.
# This is the second, independent lock on that decision — the first is that
# no automated workflow ever writes --prod into the command it runs at all
# (test_ci_workflows.py greps auto-release-staging.yml for the literal
# string) — so a bug that DID add it here still cannot promote production.
if [ "$AUTO" = "1" ] && [ "$PROD" = "1" ]; then
  echo "cut_release: --auto and --prod cannot be combined — automatic releases are staging-only (owner decision 2026-09-17, #860)" >&2
  exit 2
fi
# deploy-release.yml requires a stable vX.Y.Z tag; a malformed one would be
# pushed (the lock!) and then rejected downstream, wasting the number (review).
echo "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || { echo "cut_release: $TAG is not vX.Y.Z" >&2; exit 2; }
# --prs is no longer required here (#855 A3): an empty value means "derive it
# from every merge since the last release tag", done below once main is
# current. It is still validated non-empty before use either way.
[ -n "$MESSAGE" ] || { echo "--message is required" >&2; exit 2; }

fail() { echo "cut_release: $*" >&2; exit 1; }
note() { echo "  $*"; }

# vX.Y.Z -> vX.Y.(Z+1). Only ever called on a value that already matched the
# vX.Y.Z regex (the CLI arg above, or a candidate this function itself just
# produced), so no further validation here.
next_patch() {
  local t="${1#v}" major minor patch
  IFS='.' read -r major minor patch <<<"$t"
  echo "v${major}.${minor}.$((patch + 1))"
}

# The newest vX.Y.Z tag reachable from $1 — the base a derived --prs range
# counts forward from. Reads the REMOTE tag list (`git ls-remote --tags`,
# already this script's pattern for the lock check) rather than a local
# `git fetch --tags`: a single diverged local tag ref (stale from an old test,
# a prior epoch, another lane's abandoned attempt) makes a blanket tag fetch
# fail outright with "would clobber existing tag" and takes the WHOLE
# derivation down with it — measured against this very checkout. The commit
# objects themselves do not need a tag ref to be present locally; they are
# already there as ancestors of main's own history, which is exactly what the
# ancestry check below relies on.
# Compared numerically (python3, already used elsewhere in this script for
# JSON), not lexically: `sort` would put v0.0.9 after v0.0.10 the moment the
# patch number needs two digits. Walks newest-first and returns the first tag
# whose commit is actually an ancestor of $1, so a same-named tag pointing
# somewhere else (a corrupted or reused ref) is skipped rather than trusted.
# An optional $2 names one tag to pass over: the tag being resumed (#913 —
# see step 2c).
newest_release_tag() {
  local target="$1" exclude="${2:-}" name sha
  while read -r name sha; do
    [ -n "$name" ] || continue
    [ "$name" != "$exclude" ] || continue
    if git cat-file -e "${sha}^{commit}" 2>/dev/null && git merge-base --is-ancestor "$sha" "$target" 2>/dev/null; then
      echo "$name"
      return 0
    fi
  done < <(git ls-remote --tags origin | python3 -c '
import re, sys
pattern = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
commits = {}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    sha, ref = line.split(None, 1)
    peeled = ref.endswith("^{}")
    name = (ref[:-3] if peeled else ref).rsplit("/", 1)[-1]
    if not pattern.match(name):
        continue
    # The peeled ^{} row is the COMMIT an annotated tag points at; the
    # unpeeled row is the tag OBJECT itself. Every tag this script creates is
    # annotated, so prefer the peeled commit sha when both rows are seen.
    if peeled or name not in commits:
        commits[name] = sha
def key(n: str) -> tuple[int, ...]:
    return tuple(int(p) for p in n[1:].split("."))
for name in sorted(commits, key=key, reverse=True):
    print(name, commits[name])
')
  echo ""
}

# #860: step 1's ls-remote check is a fast fail for an OBVIOUSLY taken number,
# not the lock — minutes elapse between it and this function (PR verification,
# the main-HEAD CI wait), long enough for another lane's release to land in
# that window (three collisions in one day: v0.0.63/66/67). Re-verify
# immediately before the push that actually claims the number and, on a
# genuine race, roll TAG forward to the next free vX.Y.(Z+1) instead of
# failing the whole ceremony after all that verification work.
#
# The push itself — never the pre-check — is what must never be weakened
# (docs/release-protocol.md: "the tag push is the lock"). A push that fails
# with "already exists" means another lane's push landed between our
# ls-remote check and our own push; that failure, not the ls-remote read, is
# what triggers the roll-forward.
#
# Reads/writes the global $TAG so every later step (the tag-CI wait, the
# deploy dispatch, the printed --prod command) uses whichever number this
# release actually claimed.
push_tag_with_last_instant_lock() {
  local attempt push_err
  push_err="$(mktemp)"
  for attempt in $(seq 1 5); do
    if git ls-remote --tags origin "refs/tags/$TAG" | grep -q .; then
      note "last-instant check: $TAG was claimed by another release — rolling to the next number (attempt $attempt/5)"
      TAG=$(next_patch "$TAG")
      continue
    fi
    # A stale local refs/tags/$TAG — from an aborted earlier attempt, or a fetch that
    # brought another lane's tag — would make `git tag -a` fail before the push that decides
    # anything; the remote is the only authority here, so the local ref is recreated (review).
    git tag -d "$TAG" >/dev/null 2>&1 || true
    git tag -a "$TAG" "$LOCAL_MAIN" -m "$TAG_MESSAGE"
    if git push origin "$TAG" 2>"$push_err"; then
      rm -f "$push_err"
      note "$TAG pushed — the lock is claimed"
      return 0
    fi
    if grep -q "already exists" "$push_err"; then
      cat "$push_err" >&2
      git tag -d "$TAG" >/dev/null 2>&1 || true
      note "push raced another release for $TAG — rolling to the next number (attempt $attempt/5)"
      TAG=$(next_patch "$TAG")
      continue
    fi
    cat "$push_err" >&2
    rm -f "$push_err"
    # Nothing was claimed: leave no local tag behind to trip the next attempt.
    git tag -d "$TAG" >/dev/null 2>&1 || true
    fail "git push origin $TAG failed for a reason other than a tag collision"
  done
  rm -f "$push_err"
  fail "could not claim a free tag after 5 attempts starting from the requested number — check for a runaway parallel release"
}

# #940: auto-release-staging.yml pushed a tag whose own ci-required run never
# started at all — GitHub's recursive-workflow-run guard silently drops any
# `push` made with a workflow's own GITHUB_TOKEN, so `git push origin $TAG`
# succeeded, claimed the number, and then nothing was ever there to wait for.
# v0.0.87 sat 20 minutes, timed out, and was left on origin: claimed, never
# validated, never deployed. That push now uses a real user PAT instead (see
# auto-release-staging.yml), which is the actual fix; this is the cleanup for
# every OTHER way a fresh tag's own ci-required can still fail to go green
# (an actually-red run, a transient API outage, GitHub being slow) so a
# systemic failure does not silently spend the daily automatic-release cap
# (tools/auto_release.py) on dangling numbers, one per ~20-minute retry.
#
# --auto only. A manual ceremony never calls this: docs/release-protocol.md's
# "An abandoned tag costs nothing" already covers the operator case on
# purpose — an operator who chose a specific version by hand may already be
# coordinating around that exact number elsewhere, and this repository never
# deletes a release tag a human asked for out from under them. The automatic
# path is different: nothing outside this run has seen or can reference a
# number it alone chose, so there is nothing to lose by releasing it back.
#
# Only reachable from the branch that just pushed a NEW tag this run (not a
# --resume of one already on origin, and not after the tag's own ci-required
# already went green — see the two call sites below): a tag with green CI is
# a real, resumable release (`--redeploy` exists precisely to pick it back
# up), never something this function should delete.
abandon_fresh_tag_on_origin() {
  { [ "$AUTO" = "1" ] && [ "$FRESHLY_TAGGED" = "1" ]; } || return 0
  note "releasing $TAG on origin (--auto): its own ci-required never went green, so nothing was ever deployed against it (docs/release-protocol.md)"
  if git push origin ":refs/tags/$TAG" 2>&1 | sed 's/^/  /' >&2; then
    note "$TAG removed from origin"
  else
    echo "cut_release: could not delete origin tag $TAG — remove it by hand: git push origin :refs/tags/$TAG" >&2
  fi
  git tag -d "$TAG" >/dev/null 2>&1 || true
}

echo "== preconditions for $TAG =="

# 1. The tag push is the lock; check the lock first so a taken number fails in
#    seconds, not after minutes of PR verification. --resume/--redeploy (#811)
#    treat an existing tag as this release already in flight rather than a
#    collision — but only once step 2 below confirms it points at local main;
#    otherwise the lock still fires exactly as it always has.
[ "$REDEPLOY" = "1" ] && [ "$RESUME" = "1" ] && fail "--resume and --redeploy are exclusive — redeploy already implies the tag is done"
TAG_EXISTS=0
if git ls-remote --tags origin "refs/tags/$TAG" | grep -q .; then
  if [ "$RESUME" = "1" ] || [ "$REDEPLOY" = "1" ]; then
    TAG_EXISTS=1
    note "tag $TAG exists on origin — verifying it is local main before resuming"
  else
    fail "$TAG already exists on origin — release identity is immutable; pick the next number"
  fi
elif [ "$REDEPLOY" = "1" ]; then
  fail "--redeploy requires $TAG to already exist on origin — nothing to redeploy"
else
  note "tag $TAG is free"
fi

# 2. Local main must BE origin/main; tagging a stale or diverged checkout ships
#    the wrong tree under the right name. A stale checkout fast-forwards itself
#    (v0.0.35 and v0.0.38 both died on "pull first" while a parallel lane merged
#    mid-ceremony); only true divergence still fails.
# Deliberately NOT `--tags`: a single locally diverged tag ref (stale from an
# old attempt, another lane's abandoned release) makes a blanket tag fetch
# fail outright with "would clobber existing tag" and takes this whole step
# down with it — measured against this very checkout. Nothing below needs a
# local tag REF; `newest_release_tag` reads the remote list directly.
git fetch origin -q
# Always, not only when stale (review on #721): the ceremony's git state must be
# main's regardless of whether a fast-forward turns out to be needed.
[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] \
  || fail "run the ceremony from a checkout ON main (currently $(git rev-parse --abbrev-ref HEAD))"
LOCAL_MAIN=$(git rev-parse main)
REMOTE_MAIN=$(git rev-parse origin/main)
if [ "$LOCAL_MAIN" != "$REMOTE_MAIN" ]; then
  git merge-base --is-ancestor "$LOCAL_MAIN" "$REMOTE_MAIN" \
    || fail "local main $LOCAL_MAIN diverged from origin/main $REMOTE_MAIN — resolve by hand"
  git merge --ff-only -q "$REMOTE_MAIN"
  LOCAL_MAIN=$(git rev-parse main)
  note "main fast-forwarded to ${LOCAL_MAIN:0:8}"
fi
note "main is current at ${LOCAL_MAIN:0:8}"

# 2b. --resume/--redeploy's existing tag must point at local main, or it is a
#     genuine collision — the tag push is the lock and this is the one check
#     that must never be weakened (#811).
if [ "$TAG_EXISTS" = "1" ]; then
  TAG_COMMIT=$(git ls-remote --tags origin "refs/tags/$TAG^{}" | awk '{print $1}')
  [ -n "$TAG_COMMIT" ] || TAG_COMMIT=$(git ls-remote --tags origin "refs/tags/$TAG" | awk '{print $1}')
  [ "$TAG_COMMIT" = "$LOCAL_MAIN" ] \
    || fail "$TAG already exists on origin at ${TAG_COMMIT:0:8}, not at main HEAD ${LOCAL_MAIN:0:8} — that is another release's tag, not this one resumed; release identity is immutable, pick the next number"
  note "$TAG on origin already points at main HEAD ${LOCAL_MAIN:0:8} — this is the same release"
fi

# 2c. Batching by default (#855 A3, #860): an omitted --prs is derived from
#     every squash-merge subject between the newest reachable release tag and
#     main HEAD. This is the cadence change — one release per batch of merged
#     PRs, never one per PR — v0.0.56-v0.0.60 on 2026-09-15 were five tags for
#     five PRs, each paying the full tag-CI + deploy pipeline alone.
#     `--prs` explicit still works exactly as it always has (a break-glass
#     single-PR release stays possible) and skips all of this.
#     #913: on --resume/--redeploy, $TAG is already on origin and step 2b has
#     just proved it points at main HEAD — so $TAG itself IS the newest
#     reachable release tag, and counting from it gave an empty range ("nothing
#     to release") on every resume: the v0.0.80 and v0.0.83 retries on
#     2026-09-17 both died that way. Count from the release before it instead,
#     which is the base the first attempt derived its list from. Only a
#     verified TAG_EXISTS passes over it: a fresh release keeps counting from
#     the newest tag, so a main HEAD that is already released still fails here
#     as nothing to release.
if [ -z "$PRS" ]; then
  RESUMED_TAG=""
  [ "$TAG_EXISTS" = "1" ] && RESUMED_TAG="$TAG"
  BASE_TAG=$(newest_release_tag "$LOCAL_MAIN" "$RESUMED_TAG")
  [ -n "$BASE_TAG" ] \
    || fail "no prior vX.Y.Z release tag is reachable from main to derive --prs from — pass --prs explicitly for a first release"
  echo "== deriving --prs: every merge on main since $BASE_TAG =="
  SUBJECTS=$(git log "${BASE_TAG}..${LOCAL_MAIN}" --format=%s --reverse)
  [ -n "$SUBJECTS" ] || fail "no commits between $BASE_TAG and main HEAD ${LOCAL_MAIN:0:8} — nothing to release"
  DERIVED_PRS=()
  while IFS= read -r SUBJECT; do
    if [[ "$SUBJECT" =~ \(#([0-9]+)\)$ ]]; then
      DERIVED_PRS+=("${BASH_REMATCH[1]}")
      note "#${BASH_REMATCH[1]}: $SUBJECT"
    else
      fail "commit '$SUBJECT' since $BASE_TAG has no trailing (#N) — not a squash-merge this script can attribute to a PR; pass --prs explicitly to describe this release"
    fi
  done <<<"$SUBJECTS"
  PRS=$(IFS=,; echo "${DERIVED_PRS[*]}")
  note "derived --prs $PRS (${#DERIVED_PRS[@]} PR(s) since $BASE_TAG)"
else
  note "using explicit --prs $PRS"
fi

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

# The full PR list is release evidence, and there is nowhere else to put it:
# the infra2 DeployRequest wire contract (tools/app_deploy_request.py) has no
# field for it, and changing that contract is out of scope here — it is
# infra2's shared shape, used by more than this repository. So it lives in the
# one place this ceremony fully controls: the tag's own annotation, plus this
# run's own log (already printed per-PR above).
TAG_MESSAGE="$MESSAGE

PRs: $PRS"
# tools/auto_release.py's daily cap counts exactly this line (AUTO_TRAILER) on
# a tag's annotation — never the invocation, since only the pushed tag itself
# survives to the next run's `git for-each-ref`. A hand-cut release never
# carries it, so an operator release stays outside the automatic budget.
if [ "$AUTO" = "1" ]; then
  TAG_MESSAGE="$TAG_MESSAGE

Release-Trigger: auto-staging"
fi

# 4. main HEAD's ci-required is green — the tag inherits this SHA. A freshly
#    merged HEAD has CI still running; wait bounded instead of failing on the
#    spot (v0.0.35's first attempt died here five minutes after its merge).
MAIN_RUN=""
for _ in $(seq 1 90); do
  STATE=$(gh run list --repo "$REPO" --workflow ci-required.yml --limit 20 \
    --json databaseId,headSha,status,conclusion \
    -q "[.[]|select(.headSha==\"$LOCAL_MAIN\")][0] | \"\(.databaseId) \(.status) \(.conclusion)\"")
  case "$STATE" in
    *"completed success") MAIN_RUN=$(echo "$STATE" | awk '{print $1}'); break ;;
    *completed*) fail "ci-required for main HEAD ${LOCAL_MAIN:0:8} finished non-green: $STATE" ;;
  esac
  sleep 10
done
[ -n "$MAIN_RUN" ] || fail "ci-required for main HEAD ${LOCAL_MAIN:0:8} not green after 15 minutes"
note "main HEAD green (run $MAIN_RUN)"

if [ "$DRY" = "1" ]; then
  if [ "$REDEPLOY" = "1" ]; then
    echo "== dry run: would redeploy $TAG (already tagged + green) to staging$([ "$PROD" = "1" ] && echo ', then prod') =="
  elif [ "$RESUME" = "1" ] && [ "$TAG_EXISTS" = "1" ]; then
    echo "== dry run: would resume $TAG from the tag ci-required wait, deploy staging$([ "$PROD" = "1" ] && echo ', then prod') =="
  else
    echo "== dry run: would tag ${LOCAL_MAIN:0:8} as $TAG (PRs: $PRS), deploy staging$([ "$PROD" = "1" ] && echo ', then prod') =="
  fi
  exit 0
fi

if [ "$REDEPLOY" = "1" ]; then
  # #811: the release is live and its tag CI is green — only the deploy run's
  # gate flaked. Require green here rather than waiting for it; a wait would
  # mask the case where the tag CI was never actually green.
  echo "== redeploy: tag and tag ci-required already required green =="
  TAG_RUN=$(gh run list --repo "$REPO" --limit 30 --json databaseId,headBranch,event,status,conclusion \
    -q "[.[]|select(.headBranch==\"$TAG\" and .event==\"push\")][0] | \"\(.databaseId) \(.status) \(.conclusion)\"")
  case "$TAG_RUN" in
    *completed\ success) ;;
    *) fail "--redeploy requires a green tag ci-required run for $TAG; found: ${TAG_RUN:-none}" ;;
  esac
  TAG_RUN_ID=$(echo "$TAG_RUN" | awk '{print $1}')
  note "tag run $TAG_RUN_ID already green — skipping straight to the staging deploy"
else
  if [ "$RESUME" = "1" ] && [ "$TAG_EXISTS" = "1" ]; then
    echo "== resume: $TAG already pushed — continuing from the tag ci-required wait (#811) =="
  else
    echo "== tagging =="
    push_tag_with_last_instant_lock
    FRESHLY_TAGGED=1
  fi

  echo "== waiting for tag ci-required =="
  TAG_RUN=""
  for _ in $(seq 1 120); do
    TAG_RUN=$(gh run list --repo "$REPO" --limit 30 --json databaseId,headBranch,event,status,conclusion \
      -q "[.[]|select(.headBranch==\"$TAG\" and .event==\"push\")][0] | \"\(.databaseId) \(.status) \(.conclusion)\"")
    case "$TAG_RUN" in
      *completed\ success) break ;;
      *completed*) abandon_fresh_tag_on_origin; fail "tag ci-required failed: $TAG_RUN" ;;
    esac
    sleep 10
  done
  # The loop can also end by exhausting its budget with the run still pending, or never
  # seen: only a run that finished green may become the deploy's source_run_id (review).
  case "$TAG_RUN" in
    *completed\ success) ;;
    *) abandon_fresh_tag_on_origin
       fail "tag ci-required for $TAG is not green after 20 minutes (last seen: ${TAG_RUN:-nothing}) — not dispatching a deploy on it" ;;
  esac
  TAG_RUN_ID=$(echo "$TAG_RUN" | awk '{print $1}')
  if [ -z "$TAG_RUN_ID" ]; then
    abandon_fresh_tag_on_origin
    fail "tag run never appeared"
  fi
  note "tag run $TAG_RUN_ID green"
fi

deploy() { # $1=staging|prod, extra -f args after
  local TYPE="$1"; shift
  # A blind `sleep 12` here sometimes read the run list before GitHub's API had
  # registered the new run, and on a retry could pick up a stale run instead
  # (#811). Poll for the run this dispatch actually created — matched by its
  # run-name (`Deploy $TYPE $TAG`, see deploy-release.yml) and creation time —
  # bounded at 60s, failing loudly rather than guessing.
  local DISPATCHED_AT
  # 90 s of slack against clock skew between this machine and GitHub: a run created
  # "before" a fast local clock must still be found.
  DISPATCHED_AT=$(python3 -c 'import datetime as d;print((d.datetime.now(d.UTC)-d.timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%SZ"))')
  gh workflow run deploy-release.yml --repo "$REPO" \
    -f deploy_type="$TYPE" -f version_ref="$TAG" -f source_run_id="$TAG_RUN_ID" "$@" >/dev/null
  local TITLE="Deploy $TYPE $TAG"
  local RUN=""
  for _ in $(seq 1 12); do
    RUN=$(gh run list --repo "$REPO" --workflow deploy-release.yml --limit 10 \
      --json databaseId,displayTitle,createdAt \
      -q "[.[]|select(.displayTitle==\"$TITLE\" and .createdAt>=\"$DISPATCHED_AT\")][0].databaseId // empty")
    [ -n "$RUN" ] && break
    sleep 5
  done
  [ -n "$RUN" ] || fail "$TYPE deploy dispatch for $TAG never appeared in the run list after 60s"
  for _ in $(seq 1 120); do
    local S
    S=$(gh run view "$RUN" --repo "$REPO" --json status,conclusion -q '"\(.status) \(.conclusion)"')
    case "$S" in
      completed\ success) echo "$RUN"; return 0 ;;
      completed*) fail "$TYPE deploy run $RUN: $S" ;;
    esac
    sleep 10
  done
  fail "$TYPE deploy run $RUN timed out"
}

probe() { # $1=base url — the deployed identity must BE the tag (rule 6: serving, not tagged)
  local GOT
  GOT=$(curl -s --max-time 25 "$1/api/health" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("git_sha",""))' 2>/dev/null || true)
  [ "$GOT" = "$TAG" ] || fail "$1 serves ${GOT:-nothing}, expected $TAG"
  note "$1 serves $TAG"
}

# #855/#860: the surface walk is no longer part of the deploy run itself — it
# is a separate workflow (walk-release.yml) that deploy-release.yml's own
# completion triggers. "Staging verified" therefore now needs TWO facts, not
# one: the deploy run green (probe() above) AND a green walk run for this
# exact tag. Polls walk-release.yml's OWN runs, matched the same way
# tools/walk_evidence.py matches them (by its "Walk Deploy <type> <tag>"
# run-name) — a walk flake here prints the one-line re-run instead of this
# script re-dispatching the whole deploy again (#811's actual cost).
wait_for_walk() { # $1=staging|prod
  local TYPE="$1"
  local TITLE="Walk Deploy $TYPE $TAG"
  local RUN=""
  # workflow_run has to be delivered and queued after the deploy run finishes;
  # give it the same slack deploy()'s own dispatch-registration wait uses.
  for _ in $(seq 1 18); do
    RUN=$(gh run list --repo "$REPO" --workflow walk-release.yml --limit 20 \
      --json databaseId,displayTitle,createdAt \
      -q "[.[]|select(.displayTitle==\"$TITLE\")] | sort_by(.createdAt) | last | .databaseId // empty")
    [ -n "$RUN" ] && break
    sleep 10
  done
  [ -n "$RUN" ] || fail "no '$TITLE' run appeared within 3 minutes of the $TYPE deploy — check walk-release.yml's workflow_run trigger, or re-run by hand: gh workflow run walk-release.yml -f deploy_type=$TYPE -f version_ref=$TAG"
  for _ in $(seq 1 60); do
    local S
    S=$(gh run view "$RUN" --repo "$REPO" --json status,conclusion -q '"\(.status) \(.conclusion)"')
    case "$S" in
      completed\ success) note "walk run $RUN green for $TITLE"; return 0 ;;
      completed*)
        echo "cut_release: surface walk for $TITLE (run $RUN) is not green: $S" >&2
        echo "cut_release: re-run just the walk, not the deploy:" >&2
        echo "  gh workflow run walk-release.yml -f deploy_type=$TYPE -f version_ref=$TAG" >&2
        exit 1
        ;;
    esac
    sleep 10
  done
  fail "walk run $RUN for $TITLE did not complete within 10 minutes"
}

echo "== staging =="
STAGING_RUN=$(deploy staging | tail -1)
probe "$STAGING_URL"
note "staging run $STAGING_RUN green (fact 1 of 2)"
if [ "$AUTO" = "1" ]; then
  # #945: --auto's deploy dispatch above runs under github.token
  # (auto-release-staging.yml) — GitHub never cascades a workflow_run trigger
  # for anything a workflow's own GITHUB_TOKEN set in motion (the same rule
  # #940 hit one hop earlier, on the tag push itself), so deploy-release.yml's
  # completion would never fire walk-release.yml's workflow_run trigger and
  # wait_for_walk below would always burn its full 3-minute budget waiting for
  # a run that can never appear. Dispatch the walk explicitly instead of
  # waiting on a cascade this token cannot produce. The non-auto (real
  # operator PAT) path is unchanged: a real PAT DOES cascade, so dispatching
  # explicitly here too would walk the same deploy twice.
  note "--auto: dispatching the staging walk explicitly (github.token does not cascade workflow_run)"
  gh workflow run walk-release.yml --repo "$REPO" -f deploy_type=staging -f version_ref="$TAG" >/dev/null
fi
echo "== staging surface walk =="
wait_for_walk staging
note "staging verified: deploy green AND surface walk green for $TAG (fact 2 of 2)"

if [ "$PROD" = "1" ]; then
  echo "== prod =="
  PROD_RUN=$(deploy prod \
    -f staging_run_url="https://github.com/$REPO/actions/runs/$STAGING_RUN" \
    -f reviewed_change_url="https://github.com/$REPO/pull/$REVIEWED_PR" | tail -1)
  probe "$PROD_URL"
  note "prod run $PROD_RUN"
else
  echo "staging verified (deploy green AND surface walk green); promote with:"
  echo "  tools/cut_release.sh $TAG --prs \"$PRS\" --message \"...\" --prod  # staging_run=$STAGING_RUN"
fi
echo "== done =="
