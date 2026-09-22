# Release protocol

Written after a release that took a day and four failed dispatches, and after a
version number was claimed twice by two agents working in parallel.

## Claim the version first, push the tag immediately

Decide the version, create the annotated tag, and **push it before doing
anything else**. Not after CI, not after staging, not after the deploy — the
push is the first outward action of a release.

The tag push is the lock. `git push origin vX.Y.Z` fails when the ref already
exists, so two agents cannot claim the same version: the loser sees

```
! [rejected]        v0.0.21 -> v0.0.21 (already exists)
```

and takes the next number. That is exactly what happened on 2026-08-17 — a
concurrent agent tagged v0.0.21 while this lane was preparing the same release,
and the collision was visible immediately instead of producing two different
artifacts under one name.

**Never move a pushed tag.** `deploy-release.yml` calls its target an
"Immutable release promotion target", and infra2 diffs production cumulatively
from the marker it names. A tag that is deployed and later moved makes every
subsequent promotion diff from a baseline that no longer exists. When a tag
turns out to be short of what you wanted — v0.0.21 stopped seven commits before
main — cut the next number, say so in its message, and leave the old one alone.

## The lock is checked twice, but only one check is the lock

v0.0.63/66/67 collided with another lane THREE times on 2026-09-16 (#860):
`tools/cut_release.sh` checked `git ls-remote --tags origin` first, then spent
minutes verifying named PRs and waiting for main's `ci-required` to go green,
then tagged — long enough for another release to claim the same number in
between, so the whole ceremony died on a race the first check could not see
coming.

The fix is not a bigger or earlier check; a check can never close the window
between itself and the push that follows it. `cut_release.sh` re-verifies
`git ls-remote --tags origin` immediately before the push that actually claims
the number, and rolls forward to the next free `vX.Y.(Z+1)` — re-verifying
that one too — on either signal: the immediate re-check finding the number
taken, or the push itself failing with `already exists`. The push failure is
authoritative; the re-check is only there to skip a doomed `git tag` when the
collision is already obvious. Bounded at 5 attempts, each logged, so a runaway
parallel-release storm fails loudly instead of retrying forever.

The step-1 check from the section above stays exactly as it was: a fast, cheap
fail for a number that is obviously already taken, before minutes of PR
verification are spent on a release that cannot land under this name anyway.
Neither check is ever allowed to become the actual lock — the push is, and
that never changes.

## An abandoned tag costs nothing

v0.0.21 was tagged and never deployed. That is untidy and it is not a leak: a
tag that no environment serves holds no resource, blocks no lane, and the
freshness guard judges what production *serves*, never what tags exist.

## Parallel releases do not deadlock

`deploy-release.yml` sets

```yaml
concurrency:
  group: truealpha-release-${{ inputs.deploy_type }}-${{ inputs.version_ref }}
  cancel-in-progress: false
```

The version is part of the key, so two different releases never queue behind
each other. Two dispatches of *the same* type and version do queue, and they
wait rather than cancel — which is what you want when the second one is a
retry of a deploy whose outcome is unknown.

There is no lock held across the rest of the flow. If a release is abandoned
after the tag is pushed — CI red, an evidence gate refuses, the agent stops —
nothing is left held. The next release picks the next number and proceeds.

## Cadence: one release per merge window, not one per PR

Default cadence is batched (#855 A3, #860). `tools/cut_release.sh --prs` is
optional: omitted, the PR list is derived from every squash-merge subject
between the newest reachable release tag and main HEAD, printed for review,
and recorded in the tag's own annotation (the infra2 `DeployRequest` wire
contract has no field for a PR list, and changing a contract shared with other
repositories is out of scope for a release-cadence change — the tag message
and this ceremony's own log are the record instead). Explicit `--prs` keeps
working exactly as before, so a break-glass single-PR release stays possible
whenever one specific change needs to ship alone.

The reason is the pipeline's own fixed cost, not the change size: v0.0.56
through v0.0.60 on 2026-09-15 were five tags for five separate PRs, each
paying tag `ci-required` plus the full staging deploy alone — the same ~10 min
whether the tag describes one commit or ten. Batching a merge window's PRs
into one release pays that cost once. The last-merged PR in the batch must
still be the one whose merge commit is main HEAD (unchanged, see
`test_release_script_reviews_the_pr_that_produced_the_release_sha`) — that is
what `deploy-release.yml`'s prod gate reviews, and it is always the newest PR
in the derived range by construction.

A resumed ceremony (`--resume` / `--redeploy`, #811) derives the same batch as
the attempt it resumes (#913). Its tag is already on origin at main HEAD, so
the derived range starts at the release before that tag, not at the tag
itself. Counting from the tag itself gave an empty range, and the v0.0.80 and
v0.0.83 retries on 2026-09-17 both failed with "nothing to release". Only a
tag that is already verified to sit at main HEAD is skipped this way. A new
number cut at an already-released HEAD still has nothing to release.
`libs/runtime/tests/test_cut_release.py` runs the script end to end against a
throwaway origin to check this.

## Staging cuts itself; production still does not

On 2026-09-17 green merges to main sat untagged for up to 16.5 minutes because
nobody ran this ceremony by hand (#860). `auto-release-staging.yml` now runs
`tools/cut_release.sh --auto` for you, on one condition the owner set that day:
"先在 staging 做吧，prod 回头再说" — staging only, full stop. Production still
moves only on a deliberate `--prod` run; nothing about the automatic path
changes that.

The workflow triggers on every green `ci-required` push to main, then waits
20 minutes before it does anything — not a courtesy delay, a debounce. A
second merge landing in that window does not queue a second release behind
the first: it lands on main, and the FIRST push's wait wakes up to find main
has moved past the commit that started it. `tools/auto_release.py` (the
decision, not the trigger) checks GitHub for the CURRENT main HEAD every time
it runs, never trusts what triggered it, and stands down the moment the two
disagree — that later push already started its own independent wait, and
batching one release out of a burst of merges falls out of the same
mechanism as the cadence rule above, not a separate scheduler.

Five more reasons stand between "quiet" and "tagged", checked in this order,
first match wins (`tools/auto_release.py`'s own docstring is the source of
truth if this drifts): main HEAD is not green, main HEAD already carries a
tag, a release would restart the data engine inside staging's nightly tick
window (22:45–00:00Z, `DEPLOY_LEAD` margin included), today's automatic-release
count is already at the daily cap (4, an owner-set bound — hand-cut releases
never count against it, only tags carrying the `Release-Trigger: auto-staging`
trailer `--auto` writes), or a release is already in flight (a deploy run, a
surface walk, or a tag's own `ci-required`, any of them incomplete).

`--auto` is the one new flag on `cut_release.sh`, and it does exactly two
things: it writes that trailer, and it refuses outright if `--prod` is also
given. That refusal is the second, independent lock on "staging only" — the
first is that `auto-release-staging.yml` never types `--prod` anywhere in the
file at all (`libs/runtime/tests/test_ci_workflows.py` greps the literal
string). Promotion after an automatic release is unchanged: an operator reads
staging's evidence and runs `cut_release.sh vX.Y.Z --prod` by hand, same as
after a hand-cut one. `libs/runtime/tests/test_auto_release.py` and the
`--auto`/`--prod` cases in `test_cut_release.py` hold all of this against the
unfixed code, not just the fixed one.

What counts as reaching production: only running `cut_release.sh <tag> --prod`
(which triggers `deploy-release.yml` with `deploy_type=prod` to deploy to
production) counts as a production pipeline. Normal PR merges to `main` deploy
only to staging and do not reach production; once a PR is merge-ready under
`AGENTS.md` rule 4, the agent that owns it merges it without waiting for owner
approval.

## Staging verification is two facts, not one

Before #855/#860, a release's Playwright walk ran INSIDE `deploy-release.yml`'s
own run, right after the health/data-engine confirmation — so "the staging
deploy run is green" already meant "and it was walked". A walk flake (#811,
about 1 in 5 runs until #853) failed that whole run for a reason unrelated to
the deploy, and the only recovery was re-dispatching the entire ~5-6 min infra2
deploy.

The walk now lives in its own workflow, `walk-release.yml`, triggered by
`deploy-release.yml`'s own `workflow_run` completion (`conclusion == success`)
so a failed or cancelled deploy is never walked, plus `workflow_dispatch` for a
manual re-run. This changes what `deploy-release.yml`'s own green means: it
now asserts "deployed and healthy", and no longer "and the walk that happened
to run alongside it also passed".

Because of that, "staging is verified" is now two separate facts:
`tools/cut_release.sh` waits for the staging deploy run to go green (as
before), then separately polls `walk-release.yml` for a green walk run
matching that exact tag, before it will print the `--prod` command. A red or
missing walk prints the one-line recovery instead of re-dispatching the
deploy:

```
gh workflow run walk-release.yml -f deploy_type=staging -f version_ref=$TAG
```

`tools/walk_evidence.py` (the standing daily check in `deploy-freshness.yml`,
#560) reads `walk-release.yml`'s runs the same way it used to read
`deploy-release.yml`'s — the evidence question is unchanged, only which run
answers it moved. `deploy-release.yml`'s own prod evidence gate (a green
"Deploy staging `<tag>`" run) still proves the deploy; it additionally prints
an `::warning::`, never a hard failure, when that tag's walk evidence is not
yet confirmed — hard-blocking there would reopen the exact #560 deadlock this
design exists to avoid, since the walk can legitimately still be in flight
seconds after a staging deploy finishes. `tools/cut_release.sh`'s own two-fact
wait is what actually stands between a release and `--prod` for the normal
ceremony; the workflow-level warning is a second, best-effort signal for a
direct dispatch that bypasses it.

## What actually costs time

Measured on the v0.0.22 and v0.0.23 releases, not estimated:

| Step | Time | Compressible? |
|---|---|---|
| `ci-required` on the tag | ~5–6 min, then 2.2 min, now ~1 min | Used to duplicate the run that was already green on main for the same SHA. A4 D2 (#673) replaced the suite with an attestation that an identical-SHA green main run exists; #860 replaced the rebuild-and-republish with a re-tag of the digests main already published at that SHA (`release-images` `retag`), building only an image main did not publish there. The evidence contract is untouched either time. |
| Dispatch to infra2 | ~350 s | This is infra2 performing the deploy. Not ours to compress. |
| Surface walk | ~116 s | ~21 s of it was re-downloading Chromium every run; now cached. The rest is the walk itself, 12 routes across two identities. |
| Everything else | 2–4 s each | Nothing to take. |

Failed dispatches dominated the real cost: four consecutive attempts for
v0.0.23 collided with another project's deploys, ~20 minutes lost and the lane
shut, because the receiver run was correlated by a window in time rather than
by the request id that was already in the run title. Fixed in infra2-sdk.
