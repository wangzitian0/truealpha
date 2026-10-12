---
name: swarm
description: Atomic multi-agent batch execution engine. Executes a single concurrent batch of M Interns via subagent_batch and aggregates outputs.
---

# swarm: atomic multi-agent batch engine

This skill is the low-level execution engine for parallel multi-agent sweeps.
A single Swarm is an atomic execution unit: **1 Swarm = 1 concurrent batch of M read-only Interns (`subagent_batch`)**.
Swarm does not define multi-round state machines. Caller skills (`audit`, `init`, `ssot`, `auto`) orchestrate N rounds of Swarms with custom M counts.

## Specifications and constraints

- **Worker role**: Intern (`glm-5.3-flash` exclusively).
- **Batch size (M)**: Default 10 tasks per swarm batch; global ceiling 50 concurrent. Custom M specified by caller skill (e.g. M=4 in `init`/`ssot`, M=3 in `auto`).
- **Tool profile**: Strictly read-only tools or prompt-injected source code via `subagent_batch`.
- **Reasoning depth tiers**:
  - **Fast / Swarm Scan (`mode: fast`, `thinking: disabled`)**: Default for Intern batches and parallel sweeps. Zero reasoning overhead; sub-second execution per task.
  - **Knowledge Extraction (`mode: extract`, `reasoning_effort: medium`)**: For entity extraction, relation mapping, schema parsing, and formalization. Medium chain-of-thought (~2s latency).
  - **Bench & Evaluation (`mode: bench`, `reasoning_effort: max`)**: For mathematical proofs, invariant falsification, benchmark evaluations, and SHZP recovery. Maximum reasoning depth with 120s execution budget.
- **Output contract**:
  - Dense, structured ASD-STE100 technical findings.
  - Length: 500 to 1200 tokens per Intern.
  - Anchors required: Every finding must cite exact file anchors (`file#Lxx-Lyy`) and reproducible counterexamples.
  - Zero conversational filler. Lead with conclusion.

## Atomic invocation protocol

To execute 1 Swarm, the orchestrator invokes `subagent_batch` with M tasks:

1. Prepare task array with M distinct task objects: `[{"id": "...", "prompt": "..."}]`.
2. Inject target source code, diffs, issue descriptions, and contracts directly into each task prompt (Gate 1 compliance).
3. Call `subagent_batch`.
4. Reduce outputs: Parse each Intern's verdict into a consolidated `SwarmResult`.
5. Return `SwarmResult` to the calling skill for the next state transition.

## Lean execution discipline

- **Targeted verification only**: Run focused tests on changed targets only (`pytest <file>::<test> -x`).
- **Never run full test suites** during review or confrontation rounds.
- **Physical exit codes required**: Cite actual commands and exit codes. A score or claim without a physical command is invalid.
- **Process timeout**: Child processes have OS-level timeouts (50s default). Worker log is `~/.local/state/subagent-worker/worker.log`.
