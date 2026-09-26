# Bugs, Gotchas, and Edge Cases

Document past bugs, API limits, runtime edge cases, and unexpected behaviors to avoid repeating them in future sessions.

## Structure per Incident:
```markdown
### [YYYY-MM-DD] Bug / Gotcha Title
- Symptom: What happened or failed?
- Root Cause: Why did it fail?
- Solution: What fixed or mitigated it?
- Prevention: How should agents design around this moving forward?
```

---

### [2026-09-23] GMGN API Polling Latency and Rate Limiting
- Symptom: Network request timeouts and potential rate-limiting when checking token prices in rapid succession.
- Root Cause: Direct external HTTP requests to GMGN API on every tick create latency and can trigger HTTP 429 throttling.
- Solution: Decoupled 3-second in-memory risk tick loop in `autonomous_trader.py` with exponential backoff on HTTP errors.
- Prevention: Always use cached or batched requests when querying multiple token metrics; do not spawn unthrottled concurrent requests.

### [2026-09-23] Micro-Cap Liquidity Slippage
- Symptom: Substantial negative slippage on fast market exits during high volatility.
- Root Cause: Tokens with pool liquidity under $60,000 cannot absorb market sales without significant price impact.
- Solution: Minimum pool liquidity threshold set to $60,000 USD and maximum market cap upper bound of $5,000,000 USD.
- Prevention: Reject any candidate token where liquidity depth is insufficient for fast single-transaction liquidation.

### [2026-09-23] GMGN CLI Platform Filter Parameter Lock
- Symptom: Candidate token scanner only ingested tokens from Pump.fun, ignoring Raydium and Meteora tokens.
- Root Cause: In `aitrader/app.py`, `DEFAULT_TRENDING_CMDS["sol"]` explicitly passed `--platform Pump.fun` along with custom unaccepted flags (`--platform raydium_cpmm`), causing the upstream GMGN API to collapse the filter strictly to Pump.fun.
- Solution: Removed all `--platform` CLI flags to allow the native global Solana trending query to return all DEX pools (Raydium v4, Raydium CPMM/CLMM, Meteora DLMM/DAMM, Pump.fun, and Stonkfun). Augmented with supplementary Moonshot and Letsbonk trending queries.
- Prevention: When querying multi-DEX aggregates, do not pass restrictive platform flags unless specifically scoping to a single protocol.

### [2026-09-23] Frontend DEX Badge Fallback & Backend DEX Classification Bug
- Symptom: All tokens rendered in the UI with a "PUMP" badge, even when trading on Raydium, Meteora, or Stonkfun.
- Root Cause: In `aitrader/static/index.html`, `normalize()` failed to assign `dex: dec.dex || f.dex`, leaving `t.dex` undefined. `dexBadge(dex)` defaulted `undefined` to "PUMP". Additionally, backend `detect_dex` checked `addr.endswith('pump')` before checking active exchange, mislabeling migrated tokens.
- Solution: Fixed `normalize()` to map `t.dex`, upgraded `detect_dex` to inspect `exchange`, `launchpad`, and `migrated_pool_exchange`, and added CSS styles for all DEXes including Stonkfun.
- Prevention: Always propagate all metadata fields through frontend mappers and test raw API output schemas against mapper transformations.

### [2026-09-23] Asymmetric PnL Decay via Premature Stagnation & Micro Stop-Loss
- Symptom: Win rate dropped to ~23% and bot showed negative realized PnL despite positive unrealized portfolio gains.
- Root Cause: 6-minute stagnation timeout cut winning/consolidating positions (+1% to +3%) prematurely, while a tight -4.0% stop-loss was triggered by ordinary Solana DEX pool noise/spread.
- Solution: Extended stagnation timeout to 25 minutes (decay-only, `pnl <= -1.0%`), adjusted base stop-loss to -7.5%, and implemented a 3-tier profit-taking ladder (+10% breakeven lock, +20% partial TP, +40% partial TP, +70% moonbag free-roll).
- Prevention: In volatile AMM markets, do not cut winners on short timers; use stepped profit harvesting to bank gains incrementally.

### [2026-09-23] Backend Subprocess Lock Contention & Frontend Polling Termination
- Symptom: Dashboard froze permanently with skeleton loading bars ("nothing is getting tracked").
- Root Cause:
  1. `monitor_positions` executed two synchronous CLI subprocesses (`token security` and `token price`) for each open position while holding `with ST.lock:`. With 14+ positions, this locked the entire FastAPI server for 18-35 seconds per request.
  2. When requests from the browser timed out, `scanCycle()` in `index.html` executed `bePolling = false;` in its `catch` block, permanently halting the interval polling loop until full page refresh.
  3. Positions were coupled exclusively to the heavy candidate screening cycle, preventing real-time tracking of active slots.
- Solution:
  1. Added in-memory TTL caches (`_price_cache` 25s, `_security_cache` 180s) to `LiveGMGN` and removed the global lock from `monitor_positions` and `/api/positions`. Endpoint latency dropped from 18,000ms to 17ms.
  2. Removed `bePolling = false;` from `scanCycle()` error handling so transient network blips do not permanently kill polling.
  3. Added a dedicated 3.0s `pollPositions()` loop querying `/api/positions` directly and updated `kpiData()` and `renderPositions()` to render live positions immediately without skeleton delay.
- Prevention: Never execute blocking subprocesses inside global thread locks. Never permanently deactivate client polling loops on transient HTTP errors; use resilient retries and decoupled polling intervals for fast-changing states.

### [2026-09-24] Moonshot Profit Targets, Slot Gridlock, and Advisory-Only Council Bypass
- Symptom: Realized PnL went negative (-0.2030 SOL) while Unrealized PnL was +0.28 SOL; all 5 trading slots were permanently locked, preventing screening or rotation into new breakout tokens.
- Root Cause:
  1. Stepped take-profit triggers were widened to moonshot targets (+35%, +75%, +150%), while stop-loss was -12.0%. High-momentum tokens (PURRP at +19%, GOUR at +28%) never hit +35%, so profits were never banked and decayed back down.
  2. 35-minute stagnation timeout had a loophole (`pnl <= -1.0%`), meaning flat tokens floating between -0.9% and +3% were never recycled, jamming all 5 slots.
  3. The 23-Subagent Supreme Council evaluation was advisory-only, completely bypassing sentinel vetoes and admitting high-risk tokens.
- Solution:
  1. Recalibrated profit harvest ladder to realistic DEX micro-scalp tiers: Tier 1 (+8% -> +2% breakeven floor lock), Tier 2 (+15% -> bank 50% partial profit, floor +8%), Tier 3 (+30% -> bank 30% partial profit, floor +20%), Tier 4 (+50% -> free-roll moonbag with 15% trailing stop from ATH).
  2. Tightened base stop-loss to -6.0% (and -3.5% on re-entries).
  3. Fixed stagnation timeout to 15m and recycled any token failing to reach at least +2.0% momentum.
  4. Enforced binding Council Veto (minimum 60/100 score, rejecting any candidate vetoed by Supreme Judge).
  5. Implemented Anthropic Commerce-Agent Two-Phase Staged Guardrails with Base58 Solana mint address provenance verification.
- Prevention: Never widen profit-taking thresholds beyond typical intraday candle ranges in DEX pools; always bank stepped partial profits to lock realized gains and preserve open slot capacity.

### [2026-09-24] Unpriced Zombie Slot Gridlock, High-Water Mark Retracement, & Analysis Delegate Fencing
- Symptom: Realized PnL recovery stalled because 1 of 5 trading slots was occupied by an unpriced dummy token (`TOKEN` with `entry_price: 0.0`), preventing PnL calculation, stop-loss triggering, or stagnation recycling. Winning runners (+14.4%) retraced before reaching Tier 2 (+15%).
- Root Cause:
  1. `do_buy()` in `app.py` allowed tokens with `entry_price = 0.0` to be added to `ST.positions` when price resolution failed.
  2. `autonomous_trader.py` lacked an immediate purge routine for unpriced positions, leaving them stuck in the portfolio until daemon restart or manual intervention.
  3. Holding timestamps were reset to `time.time()` on daemon restart, losing elapsed position lifecycle.
  4. Double-digit gainers (+10% to +14%) had no intermediate trailing protection between Tier 1 (+8%) and Tier 2 (+15%), allowing them to retrace down to +2.0% breakeven.
- Solution:
  1. Unmonitored and cleared the zero-price placeholder token from `positions.json`.
  2. Added validation in `do_buy()` rejecting buys with `entry_price <= 0.0` with HTTP 422.
  3. Added automatic `CORRUPTED_ZERO_PRICE_PURGE` in `monitor_and_manage_risk()` in `autonomous_trader.py`.
  4. Added High-Water Mark Trailing Stop ratcheting (`trailing_floor = max(0.06, peak_pnl - 0.04)`) for any position hitting peak PnL >= +10.0%.
  5. Restored true entry timestamps on startup from `cycles * 3.0s` to preserve lifecycle across restarts.
  6. Implemented Step 2 of the Commerce-Agent architecture bridge via `aitrader/analysis_delegate.py` with linear-time XML prompt sandboxing (`<untrusted_token_data>`), read-only tools, and Supreme Council synthesis.
- Prevention: Never persist unpriced records into state; always reconstruct lifecycle timestamps from tick cycles; guard all untrusted market data inside explicit XML boundary delimiters.

