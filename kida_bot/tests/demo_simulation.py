#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_simulation.py — GMGN Autonomous Engine Complete Life-Cycle Simulation

Zero-Network Demonstration:
Simulates real market conditions, on-chain price curves, and all 5 execution invariants:
1. Entry Trigger: Hybrid Consensus (Trending + Smart Money >= 2 + 100% Safety Pass)
2. Emergency Escape: Instant exit upon developer dump / honeypot attempt
3. Hard Stop-Loss: Immediate liquidation at -20% drawdown
4. Capital Recovery ("Sell Inits"): At +100% gain, sells 50% to bank 100% of initial SOL capital
5. Stagnation Timeout: Exits flat token after 45m to recycle capital
6. Trailing Stop: 15% peak pullback exit on remaining moonbag
"""

import time
import sys

def print_header(title):
    print("\n" + "="*72)
    print(f"  {title}")
    print("="*72)

def print_step(time_str, phase, details):
    print(f"[{time_str}] [{phase:<16}] {details}")

def run_demo():
    print_header("GMGN ROTATION BOT — FULL 4-SCENARIO LIFE-CYCLE DEMO")
    print("Initial Configuration:")
    print("  - Active Slots: 3 Concurrent Positions (0.05 SOL each)")
    print("  - Starting Wallet Risk Capital: 0.15 SOL ($17.58)")
    print("  - Safety Gates: Mint/Freeze Renounced | LP Burned | Dev Hold < 5%")
    print("  - Target Tokens: $SOLMOON, $BLEED, $RUGDEV, $SIDEWAYS\n")
    time.sleep(2)

    # -------------------------------------------------------------
    # PHASE 1: HYBRID CONSENSUS SCREENING & ENTRIES
    # -------------------------------------------------------------
    print_header("PHASE 1: SCREENING & ROTATION SLOT ENTRY")
    print_step("00:00:00", "SCREENING", "Scanning Solana live feeds for tokens meeting all 5 Quality Invariants...")
    time.sleep(1)
    print_step("00:00:02", "CONSENSUS_PASS", "$SOLMOON  | Mcap: $420k | Vol: $310k | Smart Money Buyers: 4 | LP Burned: 100%")
    print_step("00:00:03", "CONSENSUS_PASS", "$BLEED    | Mcap: $280k | Vol: $140k | Smart Money Buyers: 2 | LP Burned: 100%")
    print_step("00:00:04", "CONSENSUS_PASS", "$RUGDEV   | Mcap: $210k | Vol: $180k | Smart Money Buyers: 3 | LP Burned: 100%")
    time.sleep(1)

    print_step("00:00:05", "BUY_EXECUTE", "FILLED Slot 1: $SOLMOON  — 0.05 SOL @ $0.00100 (Anti-MEV Jito Route)")
    print_step("00:00:06", "BUY_EXECUTE", "FILLED Slot 2: $BLEED    — 0.05 SOL @ $0.00200 (Anti-MEV Jito Route)")
    print_step("00:00:07", "BUY_EXECUTE", "FILLED Slot 3: $RUGDEV   — 0.05 SOL @ $0.00050 (Anti-MEV Jito Route)")
    print_step("00:00:08", "SLOTS_FULL",  "Active Positions: 3/3 | Total Capital Deployed: 0.15 SOL | Holding for runners...")
    time.sleep(3)

    # -------------------------------------------------------------
    # PHASE 2: EMERGENCY RUG ESCAPE
    # -------------------------------------------------------------
    print_header("PHASE 2: EMERGENCY ESCAPE PROTOCOL ($RUGDEV)")
    print_step("00:02:15", "ANOMALY_DETECT", "WARNING: $RUGDEV Deployer transfers 15M tokens to Raydium pool!")
    print_step("00:02:16", "SEVERITY_SPIKE", "Contract Security Severity Score jumps from 0 to 88!")
    print_step("00:02:17", "CIRCUIT_BREAKER", "EMERGENCY_ESCAPE Triggered on $RUGDEV (Severity >= 50)")
    print_step("00:02:18", "SELL_EXECUTE",  "FLASH LIQUIDATION: 100% of $RUGDEV sold at $0.00048 (-4.0% slippage)")
    print_step("00:02:19", "CAPITAL_SAVED",  "Salvaged 0.048 SOL before full liquidity drain. Capital loss limited to 0.002 SOL ($0.23)!")
    print_step("00:02:20", "POST-MORTEM",    "Token pool drained to 0 by developer 8 seconds later. Escaped 100% loss successfully.")
    print_step("00:02:21", "SLOT_FREED",     "Slot 3 is now EMPTY (Active: 2/3). Searching next candidate...")
    time.sleep(3)

    # -------------------------------------------------------------
    # PHASE 3: RE-ENTRY INTO $SIDEWAYS
    # -------------------------------------------------------------
    print_header("PHASE 3: CONTINUOUS ROTATION RE-ENTRY")
    print_step("00:02:30", "CONSENSUS_PASS", "$SIDEWAYS | Mcap: $650k | Vol: $220k | Smart Money Buyers: 3 | LP Burned: 100%")
    print_step("00:02:32", "BUY_EXECUTE",    "FILLED Slot 3: $SIDEWAYS — 0.05 SOL @ $0.00500 (Anti-MEV Jito Route)")
    print_step("00:02:33", "SLOTS_FULL",     "Active Positions: 3/3 | Monitoring price curves and momentum...")
    time.sleep(3)

    # -------------------------------------------------------------
    # PHASE 4: HARD STOP-LOSS CUTOFF
    # -------------------------------------------------------------
    print_header("PHASE 4: HARD STOP-LOSS EXECUTION ($BLEED)")
    print_step("00:08:40", "MOMENTUM_CHECK", "$BLEED price drifts down from $0.00200 to $0.00160 (-20.0% drawdown)")
    print_step("00:08:41", "STOP_TRIGGER",   "HARD STOP-LOSS BREACHED on $BLEED (Threshold: -20.0%)")
    print_step("00:08:42", "SELL_EXECUTE",   "MARKET EXIT: 100% of $BLEED position sold at $0.00160")
    print_step("00:08:43", "LOSS_CONTAINED", "Loss strictly locked at -0.010 SOL ($1.17). Prevented further bleed.")
    print_step("00:08:44", "SLOT_FREED",     "Slot 2 is now EMPTY (Active: 2/3). Slot queued for next entry.")
    time.sleep(3)

    # -------------------------------------------------------------
    # PHASE 5: "SELL INITS" CAPITAL RECOVERY (+100% 2X)
    # -------------------------------------------------------------
    print_header("PHASE 5: 'SELL INITS' PRINCIPAL BANKING ($SOLMOON)")
    print_step("00:15:10", "SURGE_DETECT",   "$SOLMOON price explodes from $0.00100 to $0.00200 (+100.0% gain, 2x)!")
    print_step("00:15:11", "INITS_TRIGGER",  "TARGET HIT: Net gain reaches +100%. Executing 'Sell Inits' Capital Recovery...")
    print_step("00:15:12", "SELL_EXECUTE",   "LIQUIDATING 50% of tokens: Exactly 0.050 SOL returned to wallet!")
    print_step("00:15:13", "RISK_FREE_STATE","INVARIANT SATISFIED: Net Unrecovered Capital = 0.00 SOL.")
    print_step("00:15:14", "MOONBAG_ACTIVE", "Remaining 50% balance converted to risk-free 'Moonbag'.")
    print_step("00:15:15", "TRAILING_ARMED", "Trailing Stop armed with 15% pullback threshold from dynamic peak.")
    time.sleep(3)

    # -------------------------------------------------------------
    # PHASE 6: STAGNATION TIMEOUT
    # -------------------------------------------------------------
    print_header("PHASE 6: 45-MINUTE STAGNATION TIMEOUT ($SIDEWAYS)")
    print_step("00:45:00", "TIME_CHECK",     "$SIDEWAYS position has been open for 45 minutes.")
    print_step("00:45:01", "METRIC_CHECK",   "PnL is +1.8% (flat); 1-hour volume decayed by 65%. Neither TP nor SL hit.")
    print_step("00:45:02", "TIMEOUT_TRIGGER","STAGNATION_TIMEOUT TRIGGERED: Freeing dead capital to maintain rotation velocity.")
    print_step("00:45:03", "SELL_EXECUTE",   "MARKET EXIT: Sold 100% of $SIDEWAYS at $0.00509 (+1.8% gain, 0.0509 SOL returned).")
    print_step("00:45:04", "SLOT_FREED",     "Slot 3 is now EMPTY. Capital recycled with zero drawdown.")
    time.sleep(3)

    # -------------------------------------------------------------
    # PHASE 7: TRAILING STOP ON MOONBAG ($SOLMOON)
    # -------------------------------------------------------------
    print_header("PHASE 7: TRAILING STOP ON $SOLMOON MOONBAG")
    print_step("01:10:00", "PEAK_TRACK",     "$SOLMOON reaches local high of $0.00350 (+250% from entry). Dynamic peak updated.")
    print_step("01:18:20", "PULLBACK_DETECT","$SOLMOON pulls back from $0.00350 to $0.00295 (-15.7% from peak).")
    print_step("01:18:21", "TRAILING_FIRE",  "TRAILING STOP-LOSS BREACHED: Drawdown exceeds 15% tolerance.")
    print_step("01:18:22", "SELL_EXECUTE",   "FINAL LIQUIDATION: Remaining moonbag sold at $0.00295 (+195% gain on remaining shares).")
    print_step("01:18:23", "PROFIT_CAPTURED","Returned an additional 0.0737 SOL of pure profit!")
    time.sleep(3)

    # -------------------------------------------------------------
    # FINAL FINANCIAL ACCOUNTING
    # -------------------------------------------------------------
    print_header("FINAL FINANCIAL AUDIT & PERFORMANCE SUMMARY")
    print(f"{'Token':<12} | {'Initial SOL':<12} | {'Returned SOL':<12} | {'Net PnL (SOL)':<14} | {'Outcome'}")
    print("-" * 72)
    print(f"{'$RUGDEV':<12} | {'0.0500 SOL':<12} | {'0.0480 SOL':<12} | {'-0.0020 SOL':<14} | Emergency Escape Salvaged 96%")
    print(f"{'$BLEED':<12}  | {'0.0500 SOL':<12} | {'0.0400 SOL':<12} | {'-0.0100 SOL':<14} | Hard Stop-Loss (-20%)")
    print(f"{'$SIDEWAYS':<12}| {'0.0500 SOL':<12} | {'0.0509 SOL':<12} | {'+0.0009 SOL':<14} | 45m Stagnation Timeout (+1.8%)")
    print(f"{'$SOLMOON':<12} | {'0.0500 SOL':<12} | {'0.1237 SOL':<12} | {'+0.0737 SOL':<14} | Sell Inits + Trailing Moonbag (+147%)")
    print("-" * 72)
    print(f"{'TOTALS':<12}  | {'0.2000 SOL':<12} | {'0.2626 SOL':<12} | {'+0.0626 SOL':<14} | +31.3% Net Portfolio Return")
    print("=" * 72)
    print("Execution Takeaways:")
    print("  1. Emergency Escape prevented $RUGDEV from losing the entire 0.05 SOL.")
    print("  2. Hard Stop-Loss strictly capped $BLEED at 0.01 SOL.")
    print("  3. Stagnation Timeout successfully freed dead capital after 45m.")
    print("  4. 'Sell Inits' completely de-risked $SOLMOON before the final exit.")
    print("  5. The single winner (+0.0737 SOL) overpowered all combined losses (-0.0120 SOL) by 6.1x!\n")

if __name__ == "__main__":
    run_demo()
