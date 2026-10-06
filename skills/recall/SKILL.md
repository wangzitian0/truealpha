---
name: recall
description: Search the persistent cross-agent memory before work. Use at session start, when you take a new task, or when a question depends on a past decision, pitfall, or convention.
---

# recall: look up before you act

The commands for this environment are in `local.md`. This file holds only when to recall and how to use the result.

## When

1. Session start or a new repository: read the latest architecture facts first.
2. A hard bug or an architecture change: search by keyword for past pitfalls (deadlock, config key, permission boundary).
3. The owner asks "how did we decide X" or "what did we find last time": recall first, then answer. Never invent history.

## How to use results

1. **Empty result:** continue silently. Report "no memory found" only when the owner asks.
2. **Cite briefly:** take the conclusion, not the whole entry. Keep the entry id or date. A memory without a source equals an invention.
3. **Code wins over memory.** Memory records a past decision. The code is the present fact. On conflict, follow the code and report the drift.
4. **Anchor small-model screening.** When a light model screens notes or a knowledge base, its output must carry anchors
   (`file#Lxx-Lyy` or a note id), not paraphrase only. Before a design decision, read 1 to 2 anchors yourself.
   Lossy summaries drop negative constraints and invite fake citations.
