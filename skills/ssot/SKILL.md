---
name: ssot
description: Step 3 of the five-step flow. Delete first, then converge on one source of truth. Use for refactors, dead-code removal, contracts, projections, and drift.
---

# ssot: delete, then keep one source

Order matters (Musk's steps 2 and 3): delete the part, then simplify what remains.
Never optimize a step that should not exist.

## Rules

1. **Delete before you add.** The owner asked for "is this over-designed?" and "which existing logic must be cleaned?" in several reviews.
   For each shim, adapter, or flag, ask "what breaks if I remove it?" If nothing breaks, remove it.
   If nothing you removed needs to come back later, you removed too little.
2. **One fact, one source.** Name the single editable source before you write code. Close every other edit point.
   Derived files carry `DO NOT EDIT`, come from a one-way tool, and can be deleted and rebuilt at any time.
   Never hand-edit a derived file. A hand edit is a drift candidate: move it into the source.
3. **Remove the predecessor in the same change.** After the new mechanism works and equivalence is proved, delete the old code,
   old files, and old docs in the same PR. No `@deprecated`, no "delete later".
4. **Re-aim every guard after a deletion.** A guard written for the old structure can stay green and check nothing.
   Make the guard fail on the old shape first, then repoint it.
5. **Read the original intent first.** An agent once changed a mapping without asking why it was split from the content source.
   Read the issue that created the structure. Keep a split that has a reason.
6. **Keep global configuration minimal.** Each directory reads its own rendered artifact. A tool reads only that artifact.
   Global user directories belong to user-managed tools. A workspace script must not write there.
7. **Check reachability.** From the entry points (main, routes, CLI commands), compute what is called.
   Written but never read is waste: delete it. Read but never written is a defect: fix it.
8. **Contract first.** Lock signatures, schemas, and the observable endpoint before the internals.
   Prove equivalence with real inputs on old and new paths. Run staging first when a scheduler or alert stack changes.

## SSOT Swarm (10-Intern deterministic parallel drift and dead-code sweep)

Before major refactorings or cleanups, dispatch a deterministic 10-Intern scan via `subagent_batch`. The Director dynamically defines the focus dimensions and allocation across the following baseline topology:
- **Intern 1 (Call graph reachability)**: Scans public entry points vs call graph to locate uncalled dead code and unused modules.
- **Intern 2 (Multi-source configuration drift)**: Scans for duplicate configuration definitions between Host, Repo, and App layers.
- **Intern 3 (Orphan links & rotting documentation)**: Runs link checkers (`python -m tools.doc_link_check`) to find broken documentation links and stale claims.
- **Intern 4 (Empty guard protection)**: Verifies that existing assertions and guards actually fail on deleted or mutated inputs.
- **Intern 5 (Deprecated shims & backward-compatibility wrappers)**: Finds temporary adapters and shims whose replacement has landed.
- **Intern 6 (Stale schema & contract duplication)**: Scans for duplicate DTO models, redundant interfaces, or manual copies of generated types.
- **Intern 7 (SQL query fragmentation)**: Locates fragmented database queries across consumer layers that bypass shared query services.
- **Intern 8 (Hardcoded magic numbers & constants)**: Scans for scattered thresholds, timeouts, or formulas that lack a single source of truth.
- **Intern 9 (Documented vs implemented invariant drift)**: Compares architectural documentation against reality to eliminate false defect claims.
- **Intern 10 (Dead test assertions & tautologies)**: Inspects test fixtures for deleted code paths or assertions that run on empty result sets.

## Report

State what you deleted, what is now the single source, and the check that fails if a second copy appears.
