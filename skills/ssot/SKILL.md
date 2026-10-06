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

## Report

State what you deleted, what is now the single source, and the check that fails if a second copy appears.
