# Pre-push self-review — the six categories that were 90% of review findings

Tallied from the 14 actionable Copilot findings across five 2026-08-28 PRs
(#675, #677, #679, #683, #689). Each round a finding forced cost ~10–15 min
(fix, push, re-CI, thread). Applying this list to the diff BEFORE pushing is the
cheapest speed lever the pipeline has left: it attacks round count, which now
dominates iteration time, not CI duration.

Read the full diff once per category. Every entry names the PR where it bit.

## 1. Fragile parsing (4 findings)
- Splitting on a token that also appears in prose or flags: `split()[3:]`
  counted `-q` as a path (#675); splitting SQL on the first `from ` matched a
  comment (#652-era, third scanner that week).
- `grep | head` under the runner's `pipefail` dies of SIGPIPE past N matches
  and takes the fallback exactly when there is the most to report (#689).
- A flag's value read by index: `--root-path` last on the line raised
  IndexError instead of a sentence (#677); `--root-path=/api` parsed as a
  different value.
- A perl/sed delimiter that also appears in the pattern: `s|…\|\|…|…|`
  unescapes `\|` at the delimiter layer, leaving a bare `|` alternation with an
  EMPTY branch — the empty branch matches at position 0 of every line and the
  replacement lands 310 times (v0.0.34 session, caught by redprove's inert
  verdict). Pick a delimiter absent from both halves, or wrap the pattern in
  `\Q…\E` and match the shortest distinctive literal instead of the whole line.

## 2. Failure that names its contract (3)
- Bare `next(...)` → StopIteration instead of "the changes job no longer
  carries a filters step" (#683).
- `headers["location"]` on a 405 with no Location → KeyError burying WHICH
  method broke (#669).
- `assert` validating external data disappears under `python -O` (#677).

## 3. Stale or misleading prose (3)
- A comment naming the wrong enforcement file after the check moved (#675).
- PR-edit archaeology in a code comment instead of the commit message (#683).
- A docstring conflating Dockerfile and Compose entrypoint semantics (#677).

## 4. Injection and quoting (2)
- A value interpolated into a remote shell line: whitelist it, don't out-quote
  it (#677). A Location built from `request.url.netloc` is a host-header
  redirect (#665).

## 5. Fail-open on the degraded path (2)
- `|| true` on a listing call turned a rate limit into duplicate issues (#689).
- `last:50` without totalCount passed by omission past one page (#677).

## 6. Permission and blast radius (1)
- `issues: write` at workflow level on a workflow that also runs on
  pull_request: the write token reached PR-controlled code (#689).

## The two mechanical rules that precede all six
- After every scripted edit: assert a NON-EMPTY diff (`git diff --stat` or
  redprove's built-in check). Four silent no-op edits in one session, one of
  which led to answering a review thread "fixed" while nothing had changed.
- Red cases run through `tools/redprove.sh`, never the hand dance: it enforces
  branch state, edit-landed, expected-assertion-matched, and restore.
