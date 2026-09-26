---
name: agent-memory
description: Persistent long-term memory engine backed by a local Markdown vault and SQLite FTS5 index. Use to store and recall historical decisions, architecture layouts, fixed bugs, and user preferences across sessions.
triggers:
  - remember
  - recall
  - memory
  - store memory
  - recall memory
  - agent memory
  - long term memory
  - search memory
---

# Agent Long-Term Memory Skill

This skill allows agents to store and retrieve persistent knowledge across sessions, preventing context degradation and loss of historical technical context.

## Storage Backends

1. **Structured Markdown Vault**: Located at `.agent_memory/` (`decisions.md`, `architecture.md`, `bugs_and_gotchas.md`, `session_log.md`).
2. **SQLite FTS5 Index**: Located at `.agent_memory/memory.db`.

## Execution Workflows

### 1. Session Bootstrap / Context Recall
Whenever starting work on a new feature, debugging an issue, or researching past strategies, query the memory index first:

```bash
python3 .agents/skills/agent-memory/scripts/memory.py recall "<search_query>"
```

To filter by category (`decision`, `architecture`, `bug`, `preference`, `session`):
```bash
python3 .agents/skills/agent-memory/scripts/memory.py recall "<search_query>" --category <category>
```

To fetch a complete record by key:
```bash
python3 .agents/skills/agent-memory/scripts/memory.py get "<key>"
```

### 2. Checkpointing Decisions & Discoveries
Whenever completing an architectural decision, discovering an API gotcha, or fixing a bug, store it in memory:

```bash
python3 .agents/skills/agent-memory/scripts/memory.py store "<key>" "<content>" --category <category> --tags "<tag1,tag2>"
```

Categories:
* `decision`: Strategic, algorithmic, and parameter selections.
* `architecture`: System components, file responsibilities, and interface boundaries.
* `bug`: Resolved runtime bugs, rate-limit traps, and edge case fixes.
* `preference`: User preferences, styling requirements, and operational rules.
* `session`: Milestones and completion checkpoints.

### 3. Markdown Vault Synchronization
To index newly edited Markdown files from `.agent_memory/` into SQLite FTS5:

```bash
python3 .agents/skills/agent-memory/scripts/memory.py sync
```

### 4. Viewing Available Memories
To inspect what is currently indexed:

```bash
python3 .agents/skills/agent-memory/scripts/memory.py list
```
