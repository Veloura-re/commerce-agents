# Agent Long-Term Memory & Continuity Invariants

## 1. Context Bootstrap Protocol
- At the start of any multi-step task, architectural change, or debugging session, the agent MUST consult `.agent_memory/` or run `python3 .agents/skills/agent-memory/scripts/memory.py recall "<topic>"` to retrieve historical decisions, API constraints, and past bug fixes.
- Do not repeat previously documented failure modes recorded in `.agent_memory/bugs_and_gotchas.md`.

## 2. Checkpoint on Task Completion
- Upon completing a major architectural milestone, resolving a subtle bug, or introducing a new system component, the agent MUST:
  1. Record the change in the appropriate `.agent_memory/` file (`decisions.md`, `architecture.md`, `bugs_and_gotchas.md`, or `session_log.md`).
  2. Execute `python3 .agents/skills/agent-memory/scripts/memory.py sync` or `memory.py store` to keep the SQLite FTS5 index current.

## 3. Preservation of Historical Context
- Never delete or truncate historical memory entries unless explicitly updating an obsolete architectural invariant.
- All stored memories, titles, tags, and logs MUST adhere strictly to the zero-emoji mandate and maintain an executive, technical tone.
