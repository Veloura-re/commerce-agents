# Agent Long-Term Memory Vault

This directory serves as the persistent, local long-term memory store for AI agents working within this workspace. It bridges the context window barrier across sessions.

## Vault Topology

* `decisions.md`: Key architectural, parameter, and algorithmic choices.
* `architecture.md`: Blueprint of active trading systems, modules, entrypoints, and data pipelines.
* `bugs_and_gotchas.md`: Recorded failure modes, API quirks, rate limits, and confirmed fixes.
* `session_log.md`: Chronological log of work milestones and active work-in-progress state.
* `memory.db`: Local SQLite database backing the `agent-memory` FTS5 search engine.

## Operational Rules for Agents

1. **Session Bootstrap**: At the start of any planning or implementation phase, read these files or execute `python .agents/skills/agent-memory/scripts/memory.py recall "<topic>"` to retrieve past context.
2. **Task Wrap-up**: Before finishing a task, record key decisions, bug fixes, or architecture updates to the corresponding file or via `memory.py store`.
3. **No Overwrites of History**: Append new observations with timestamps; do not destroy historical records unless explicitly updating an outdated invariant.
