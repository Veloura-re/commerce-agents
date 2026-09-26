# TR Kida Bot System Architecture & Codebase Map

This document records the architectural map, runtime entrypoints, core data structures, and integration boundaries for AI agents operating in this codebase.

## 1. Top-Level Directory Layout

```
tr--kida--bot--config/
├── .agent_memory/                    # Long-term memory vault (markdown + SQLite FTS5)
├── .agents/
│   ├── rules/                       # Workspace rules (e.g., memory.md)
│   └── skills/                      # Agent skills (GMGN tools + agent-memory)
├── aitrader/                        # FastAPI Trading and Screening Dashboard
│   ├── app.py                       # Core FastAPI server and live GMGN adapter
│   ├── brain.py                     # Decision and scoring engine
│   ├── requirements.txt             # Dependencies (fastapi, uvicorn)
│   ├── static/index.html            # Web dashboard (EN / CN)
│   └── outputs/                     # Runtime logs, position states, overrides
├── autonomous_trader.py             # Continuous multi-coin rotation daemon
├── replay_1h_session.py             # Historical session replay tester
├── run_instant_stress_test.py       # Stress testing engine
├── demo_simulation.py               # Dry-run paper trading simulator
├── audit_wallet.sh                  # CLI wallet auditor
├── run_autonomous_bot.sh            # Background daemon runner
└── start_trader.sh                  # Web dashboard launcher
```

## 2. Core Execution Subsystems

### A. Continuous Rotation Daemon (`autonomous_trader.py`)
* **Slot Model**: Maximum 3 concurrent active positions.
* **Capital Risk**: Fixed 0.05 SOL allocation per position (max 0.15 SOL concurrent risk).
* **Entry Filters**: Market Cap $200k–$3M, 1h Volume >= $100k, Smart Money buyers >= 2 within 15m.
* **Safety Invariants**: LP Burned 100%, Mint/Freeze revoked, Top 10 holders < 20%, Dev holdings < 5%.
* **Exit Protocols**:
  * Hard Stop-Loss: -20%.
  * Stagnation Timeout: 45m with PnL between -5% and +15%.
  * "Sell Inits" Capital Recovery: 50% sold at +100% gain (2x).
  * Trailing Stop on Moonbag: 15% pullback from peak.

### B. Dashboard Server (`aitrader/app.py` & `aitrader/brain.py`)
* FastAPI application exposing live GMGN token intelligence, wallet auditing, and trade simulation endpoints.
* Serves the bilingual dashboard located at `aitrader/static/index.html`.

### C. Agent Memory Engine (`.agent_memory/` & `.agents/skills/agent-memory/`)
* SQLite FTS5 index located at `.agent_memory/memory.db`.
* CLI interface at `.agents/skills/agent-memory/scripts/memory.py`.
* Synchronized with markdown vault files in `.agent_memory/`.
