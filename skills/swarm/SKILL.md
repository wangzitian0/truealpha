---
name: swarm
description: Universal multi-agent swarm execution engine. Coordinates 10 Interns across an adversarial state machine of up to 5 rounds (Propose -> Cross-Falsify -> Triangulate -> Extended Confrontation) with dynamic Director direction and physical reality verification.
---

# swarm: multi-agent adversarial engine

This skill is the execution engine for multi-agent parallel investigations, audits, and discovery sweeps. It coordinates batches of 10 read-only Interns (`subagent_batch`) through up to 5 structured rounds of hypothesis generation, adversarial cross-falsification, and Director physical verification. The Director dynamically defines audit dimensions and round progression.

## Specifications and constraints

- **Worker role**: Intern (`glm-5.3-flash` exclusively).
- **Concurrency**: Default 10 tasks per swarm batch; global ceiling 50 concurrent.
- **Tool profile**: Strictly read-only tools or input-injected source code via `subagent_batch`.
- **Reasoning depth tiers**:
  - **Fast / Swarm Scan (`mode: fast`, `thinking: disabled`)**: Default for Intern batches and parallel sweeps. Zero reasoning overhead; sub-second execution per task.
  - **Knowledge Extraction (`mode: extract`, `reasoning_effort: medium`)**: For entity extraction, relation mapping, schema parsing, and formalization. Medium chain-of-thought (~2s latency).
  - **Bench & Evaluation (`mode: bench`, `reasoning_effort: max`)**: For mathematical proofs, invariant falsification, benchmark evaluations, and SHZP recovery. Maximum reasoning depth with 120s execution budget.
- **Output contract**:
  - Dense, structured ASD-STE100 technical findings.
  - Length: 500 to 1200 tokens per Intern.
  - Anchors required: Every finding must cite exact file anchors (`file#Lxx-Lyy`) and reproducible counterexamples.
  - Zero conversational filler. Lead with conclusion.

## Dynamic 5-round state machine

```
Round 1 (Propose)
10 parallel Interns across Director-defined dimensions
Output: Unverified defect hypotheses with exact anchors
       │
       ▼
Round 2 (Cross-Falsify)
Interns cross-examine opposite hypotheses
Goal: Disprove claims with code counterexamples
Output: Hypotheses marked [DISPROVEN] or [CONFIRMED]
       │
       ▼
Round 3 (Director Triangulation)
Director conducts Touch Reality probes on [CONFIRMED] findings
Output: Verified findings or escalation triggers
       │
       ▼ (if contested HIGH findings remain)
Round 4 - 5 (Extended Confrontation & Final Verdict)
Targeted confrontation rounds; terminal round is audit-only
Output: Final verified verdict and action list
```

### Round 1: Propose (Massive read, high-density output)

1. The Director injects the target source code, diffs, issue descriptions, and contracts into each task prompt.
2. 10 parallel Interns inspect the system across distinct allocated dimensions (e.g. scouts M1-M4, G1-G3, T1-T2, S1).
3. Each Intern outputs dense defect hypotheses with exact `file#Lxx-Lyy` citations. Un-anchored claims are discarded immediately.

### Round 2: Cross-falsify (Adversarial confrontation)

1. The Director aggregates all Round 1 hypotheses into an audit sheet.
2. Interns are paired as opponents (Red Team vs Blue Team). Each Intern receives the opposing findings.
3. **Primary objective**: Find physical counterexamples, existing guards, or specification clauses to disprove the finding.
4. Interns classify each finding strictly as:
   - `[DISPROVEN]`: Refuted with exact counterexample anchor `file#Lxx-Lyy` and physical rationale.
   - `[CONFIRMED]`: Verified irrefutable defect after attempting to disprove it.

### Round 3: Director triangulation (Physical reality probe)

1. The Director reviews surviving `[CONFIRMED]` claims.
2. The Director executes targeted read-only commands on the real system (Touch Reality: file checks, exit codes, git status, live probes).
3. The Director rejects remaining false positives and creates the final defect action list.

## Convergence and escalation

- **Standard convergence**: A round with zero HIGH and zero new MIDDLE findings converges.
- **Maximum rounds**: If HIGH findings remain contested after Round 3, continue cross-falsification up to Round 5 at most.
- **Terminal round**: The final round is audit-only. It edits no code.

## Lean execution discipline (inherited across all rounds)

- **Targeted verification only**: Run focused tests on changed targets only (`pytest <file>::<test> -x`).
- **Never run full test suites** during review or confrontation rounds.
- **Physical exit codes required**: Cite actual commands and exit codes. A score or claim without a physical command is invalid.
