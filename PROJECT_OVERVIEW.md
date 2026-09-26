# TR KIDA BOT CONFIG - MASTER SPECIFICATION

## 1. System Architecture

```
tr--kida--bot--config/
├── aitrader/                     # Main FastAPI Trading and Screening Dashboard
│   ├── app.py                   # Core server: rule engine, live GMGN adapter, risk limits
│   ├── requirements.txt         # fastapi, uvicorn
│   ├── static/index.html        # Interactive frontend dashboard (Bilingual EN/CN)
│   ├── WALLET_TAB_NOTES.md      # Wallet evaluation scoring architecture
│   └── outputs/                 # Decision logs, persistent positions, and overrides
├── .agents/skills/               # 14 Installed GMGN AI Agent Skills
├── autonomous_trader.py         # Continuous Non-Stop Multi-Coin Rotation Daemon
├── run_autonomous_bot.sh        # Background launcher for continuous daemon
├── start_trader.sh              # Script to launch the AI Trader dashboard
├── audit_wallet.sh              # CLI tool to audit any Solana or EVM wallet address
├── copy_trading_intelligence_dossier.md # Research report on on-chain bot archetypes
└── README.md                    # Repository documentation
```

## 2. Continuous Rotation Engine Specifications

* **Rotation Slots:** `3` concurrent active positions.
* **Allocation Sizing:** `0.05 SOL` fixed risk per slot (Total capital at risk capped at `0.15 SOL`).
* **Quality Filter ("Top Performing & Most Sure"):**
  * Market Cap: `$200,000` to `$3,000,000` (liquid enough to prevent high slippage).
  * 1-hour Volume: $\ge \$100,000$ (strong active capital velocity).
  * Smart Money Consensus: $\ge 2$ verified Smart Money buyers within 15 minutes.
  * Hard Safety Invariants: 100% LP Burned, Mint Authority Revoked, Freeze Authority Revoked, Top 10 Holders $< 20\%$, Dev Holdings $< 5\%$.
* **Exit Protocols (Capital Preservation Priority):**
  * **Emergency Escape:** Immediate liquidation if developer dumps or top 10 supply spikes above 30%.
  * **Hard Stop-Loss:** `-20%` drawdown cutoff.
  * **Stagnation Timeout (Momentum Decay):** If a coin stays flat (PnL between `-5%` and `+15%`) for 45 minutes, market sell to preserve capital and free the slot for an active runner.
  * **"Sell Inits" Capital Recovery:** At `+100%` gain (2x), liquidates 50% of the token holding to return 100% of the initial SOL invested ($\Delta C = 0$).
  * **Trailing Stop on Moonbag:** 15% pullback from peak price on the remaining 50% allocation.
* **Capital Accounting:** Fixed 0.05 SOL entry sizing; all realized profits remain banked in the wallet.

## 3. How to Run

### Start the Visual Dashboard
```bash
cd /home/liora/Documents/prjs/tr--kida--bot--config
./start_trader.sh
```
*Access in browser at:* `http://127.0.0.1:8000`

### Start the Continuous Non-Stop Rotation Bot
```bash
cd /home/liora/Documents/prjs/tr--kida--bot--config
./run_autonomous_bot.sh
```
