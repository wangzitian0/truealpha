---
name: smoke
description: Step 4 of the five-step flow. Run a fast check in about 30 seconds after an edit. Compile, focused tests, then connectivity. Not a deep audit.
---

# smoke: fast, cheap, honest

Smoke is the first guard after an edit. Use `audit` for depth and `close` for the exit gate.

## Order (left to right, fail fast)

1. **Compile or syntax** on the changed files only (`python -m py_compile`, `ruff check --select E,F`, `go build ./...`, `tsc --noEmit`).
2. **Focused tests** that touch the change, with `-x`. Never run the full suite per round.
   The owner stopped agents that waited minutes on slow CI. Cut slow tests and report what you cut.
   Never invoke whole-repository test runners (`ci_runner.py all`, `check_suite_coverage.py`) during smoke.
3. **Connectivity** of what the code depends on: credentials, services, MCP servers. Commands are in `local.md`.

Stop at the first red. Smoke reports the problem. It does not fix it, and it changes no file.

## Rules that came from failures

- **Existence is not validity.** "The variable is set" and "the process is alive" prove nothing.
  Call a read-only endpoint, or do not claim green.
- **No `grep PONG` probes.** A string match hides truncation, token overflow, and silent downgrade.
  A probe must check three things: exit code 0, wall time, and a non-empty payload of the expected shape.
- **Skipped is not passed.** Report a check that did not run as `SKIPPED`. Do not count it green.
- **Use the small model tier.** A smoke run with a large model or deep reasoning burns the rolling quota.
  Put concrete model names and flags in `local.md`. They change often.
- **Do not wait on slow CI.** When a run exceeds the budget, run the focused checks yourself and report the CI status separately.

## Report

One table, one row per check: result (`PASS`, `FAIL`, `SKIPPED`), seconds, and the count (`N passed`).
Add the total time and one sentence: which row is red.
