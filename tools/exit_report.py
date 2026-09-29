#!/usr/bin/env python3
"""
KIDA Citadel - Exit Reason & Settled Trade Attribution Report
Groups settled trades by exit reason (rapid cut, bleeder, hard stop, ratchet tiers, stagnation, green vault).
Audits PnL, win rates, fee drag, and slippage attribution across exit archetypes.
"""

import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kida_bot.core.db import get_exit_report_data, init_db


def classify_exit_category(reason: str) -> str:
    r = (reason or "").lower()
    if "rapid" in r or "25s" in r:
        return "Rapid Cut (-2% @ 25s)"
    elif "bleeder" in r or "90s" in r:
        return "Bleeder Defense (-1.8% @ 90s)"
    elif "stop" in r or "hard" in r:
        return "Hard Stop Loss (-3.5%)"
    elif "tier1" in r or "tier 1" in r:
        return "Ratchet Tier 1 (+10% -> lock +2%)"
    elif "tier2" in r or "tier 2" in r:
        return "Ratchet Tier 2 (+20% -> lock +5%)"
    elif "tier3" in r or "tier 3" in r:
        return "Ratchet Tier 3 (+40% -> lock +15%)"
    elif "tier4" in r or "tier 4" in r:
        return "Ratchet Tier 4 (+80% -> lock +35%)"
    elif "vault" in r or "green" in r:
        return "Green Vault (+0.6% -> floor)"
    elif "stagnat" in r:
        return "Stagnation Exit"
    elif "manual" in r:
        return "Manual Discretionary Exit"
    elif "flatten" in r or "emergency" in r:
        return "Emergency Liquidation / Flatten"
    return f"Other: {reason[:28]}"


def main():
    init_db()
    rows = get_exit_report_data()

    print("=" * 105)
    print("KIDA CITADEL // SETTLED EXITS ATTRIBUTION & PNL BREAKDOWN REPORT")
    print("=" * 105)
    print(
        f"{'Exit Reason / Archetype':<32} | "
        f"{'Trades':<6} | "
        f"{'Win %':<7} | "
        f"{'Total SOL':<10} | "
        f"{'Avg SOL':<9} | "
        f"{'Avg PnL %':<10} | "
        f"{'Fees (SOL)':<10} | "
        f"{'Avg Latency':<11}"
    )
    print("-" * 105)

    if not rows:
        print("No settled trades logged in data/kida.db yet.")
        print("=" * 105)
        return

    # Categorize and aggregate
    categories = {}
    total_trades = 0
    total_sol_pnl = 0.0
    total_fees = 0.0

    for r in rows:
        cat = classify_exit_category(r["exit_reason"])
        if cat not in categories:
            categories[cat] = {
                "trades": 0, "wins": 0, "losses": 0,
                "pnl_sol": 0.0, "pnl_pct_sum": 0.0,
                "fees": 0.0, "latency_sum": 0.0
            }
        c = categories[cat]
        cnt = r["total_trades"]
        c["trades"] += cnt
        c["wins"] += (r["win_count"] or 0)
        c["losses"] += (r["loss_count"] or 0)
        c["pnl_sol"] += (r["total_pnl_sol"] or 0.0)
        c["pnl_pct_sum"] += (r["avg_pnl_pct"] or 0.0) * cnt
        c["fees"] += (r["total_fees_sol"] or 0.0)
        c["latency_sum"] += (r["avg_latency_ms"] or 0.0) * cnt

        total_trades += cnt
        total_sol_pnl += (r["total_pnl_sol"] or 0.0)
        total_fees += (r["total_fees_sol"] or 0.0)

    for cat_name, c in sorted(categories.items(), key=lambda x: -x[1]["pnl_sol"]):
        win_rate = (c["wins"] / c["trades"]) if c["trades"] > 0 else 0.0
        avg_sol = (c["pnl_sol"] / c["trades"]) if c["trades"] > 0 else 0.0
        avg_pct = (c["pnl_pct_sum"] / c["trades"]) if c["trades"] > 0 else 0.0
        avg_lat = (c["latency_sum"] / c["trades"]) if c["trades"] > 0 else 0.0

        print(
            f"{cat_name:<32} | "
            f"{c['trades']:<6} | "
            f"{win_rate * 100:>5.1f}% | "
            f"{c['pnl_sol']:>+9.4f} | "
            f"{avg_sol:>+8.4f} | "
            f"{avg_pct * 100:>+8.2f}% | "
            f"{c['fees']:>9.4f} | "
            f"{avg_lat:>8.1f} ms"
        )

    print("-" * 105)
    print(
        f"{'TOTAL PORTFOLIO SETTLED':<32} | "
        f"{total_trades:<6} | "
        f"{'--':<7} | "
        f"{total_sol_pnl:>+9.4f} | "
        f"{'--':<9} | "
        f"{'--':<10} | "
        f"{total_fees:>9.4f} | "
        f"{'--':<11}"
    )
    print("=" * 105)


if __name__ == "__main__":
    main()
