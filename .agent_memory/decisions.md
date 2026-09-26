# Architectural & Strategic Decisions

Record all major engineering decisions, trade risk policies, parameter changes, and strategic rationale here.

## Structure per Decision:
```markdown
### [YYYY-MM-DD] Decision Title
- Context: Why was this considered?
- Choice: What was chosen?
- Rationale: Why was it selected over alternatives?
- Consequences: What constraints or expectations does this introduce?
```

---

### [2026-09-23] Dual-Tier Memory System Architecture
- Context: AI agent context window limits cause degradation and loss of historical rationale across multi-turn sessions.
- Choice: Combined structured Markdown files in `.agent_memory/` with a local SQLite FTS5 database queried via the `agent-memory` skill.
- Rationale: Markdown files provide human readability and Git trackability. SQLite FTS5 provides fast, ranked full-text queries for high-precision retrieval without context window bloat.
- Consequences: All agents operating in this workspace must consult `.agent_memory/` on boot and commit state changes on task wrap-up.

### [2026-09-23] KIDA Brain Unified Citadel Architecture
- Context: Analytics, flight logs, and trade history were separated into individual top-level navigation views with redundant top-bar shortcuts, cluttering navigation and requiring excessive tab switching.
- Choice: Consolidated Gains & Losses Citadel, Flight Black Box terminal, and Trade History ledger directly into `#viewBrain`, unified with a sticky anchor subnavigation bar.
- Rationale: Streamlines top-level navigation to three core command centers (Discovery, Vault, Brain). Preserves existing telemetry polling and real-time updates while co-locating strategic analytics and audit logs with AI brain heuristics.
- Consequences: Legacy view identifiers redirect cleanly to Brain anchors; DOM IDs remain backward-compatible with automated tests.

### [2026-09-23] Sovereign Identity & Self-Operated Wallet Evaluation Engine
- Context: The user requested a dedicated page to view and evaluate their own wallet performance, active positions, risk telemetry, and on-chain identity without leaving the trading terminal.
- Choice: Implemented a dedicated top navigation tab `MY WALLET` (`#viewMyWallet`) backed by a specialized endpoint `GET /api/wallet/my`.
- Rationale: External wallet auditing tools either target generic third-party copy-trading targets or fail to reflect local quantitative execution telemetry (ratchet stop-loss, 23-sentinel guards, 50% capital reserves). Building a dedicated view gives sovereign clarity with zero latency, complete multi-chain profile switching, and instant 1-click rotation slot exits.
- Consequences: The endpoint integrates local `session_audit.json` and `positions.json` with GMGN on-chain APIs. Strict zero-emoji sanitization is applied to all on-chain tickers via regex to uphold the zero-emoji mandate.

