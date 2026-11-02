---
name: swarm
description: Atomic multi-agent batch execution engine. Executes a single concurrent batch of M Interns via native subagents or Pi and aggregates outputs.
---

# swarm: atomic multi-agent batch engine

This skill is the low-level execution engine for parallel multi-agent sweeps.
A single Swarm is an atomic execution unit: **1 Swarm = 1 concurrent batch of M read-only Interns**.
Swarm does not define multi-round state machines. Caller skills (`audit`, `init`, `ssot`, `auto`) orchestrate N rounds of Swarms with custom M counts.

## Specifications and constraints

- **Worker role**: Intern (`pi/agents/intern.md`, model `zai-coding-cn/glm-5.3-flash`).
- **Batch size (M)**: Default 10 tasks per swarm batch. Pi's `subagent` extension (`MAX_PARALLEL_TASKS=8`, `MAX_CONCURRENCY=4`) or a native subagent running concurrent `pi --mode json -p --no-session` processes manages wave concurrency. Custom M specified by caller skill (e.g. M=4 in `init`/`ssot`, M=3 in `auto`).
- **Tool profile**: Strictly read-only tools (`read`, `bash` read-only probes) via native read-only subagents or Pi `intern`.
- **Output contract**:
  - Dense, structured ASD-STE100 technical findings.
  - Length: 500 to 1200 tokens per Intern.
  - Anchors required: Every finding must cite exact file anchors (`file#Lxx-Lyy`) and reproducible counterexamples.
  - Zero conversational filler. Start line 1 with the single line-1 JSON verdict contract (`{"subject": "claim" | "code", "verdict": "holds" | "breaks" | "unknown", "anchors": [...], "unverified": [...]}`). Do not restate or invent a secondary format.

## Atomic invocation protocol

To execute 1 Swarm, the orchestrator dispatches M Intern tasks:

1. Prepare M distinct task prompts with target file paths and line ranges.
2. Dispatch via native read-only subagents or a subagent operating `pi --mode json -p --no-session --provider zai-coding-cn --model glm-5.3-flash --tools read,bash`.
3. Wait for OS process completion (host background task wakeup; zero foreground polling).
4. Reduce outputs: Persist full output to `.agents/runs/<task-id>/logs/`; fold evidence-free `holds` tasks (`unverified: []`) and expand `breaks`, `unknown`, non-empty `unverified`, or failed tasks into `SwarmResult`.
5. Return `SwarmResult` to the calling skill for the next state transition.

## Execution discipline

- **Physical exit codes required**: Cite actual commands and exit codes. A score or claim without a physical command is invalid.
- **Liveness and recovery**: Judge running workers by child process tree and log growth, not transcript silence alone. On a transient network error, resume with `pi --continue` or `pi --session <id>`.
