#!/usr/bin/env python3
"""
KIDA Citadel - Replay Harness (Phase 7)
Runs the screening + exit logic over logged candidates and price paths
to compare config variants (stops, gate thresholds) without risking capital.

Usage:
    python tools/replay.py                         # Default: use existing DB data
    python tools/replay.py --stop-loss -0.03       # Override stop loss
    python tools/replay.py --rapid-cut -0.025      # Override rapid cut PnL
    python tools/replay.py --min-liquidity 60000   # Override gate threshold
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kida_bot.core.db import get_connection, init_db


def load_candidates_with_returns(
    db_path: Optional[Path] = None,
    limit: int = 5000,
) -> List[Dict[str, Any]]:
    """Loads historical candidates with forward return data for replay."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT c.*, f.return_1m, f.return_5m, f.return_15m,
                   f.price_1m, f.price_5m, f.price_15m
            FROM candidates c
            INNER JOIN forward_returns f ON c.id = f.candidate_id
            WHERE f.completed = 1 AND f.return_5m IS NOT NULL
            ORDER BY c.ts DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]


def simulate_gate_pass(
    cand: Dict[str, Any],
    min_liquidity: float = 45000.0,
    min_mcap: float = 40000.0,
    min_buy_ratio: float = 0.50,
    max_buy_tax: float = 0.08,
    max_sell_tax: float = 0.08,
    require_renounced_freeze: bool = True,
) -> tuple:
    """Replays gate logic with overridable thresholds."""
    liq = float(cand.get("liquidity") or 0)
    if liq < min_liquidity:
        return False, f"LIQUIDITY_BELOW_{min_liquidity}"

    mcap = float(cand.get("mcap") or 0)
    if mcap < min_mcap:
        return False, f"MCAP_BELOW_{min_mcap}"

    buy_ratio = float(cand.get("buy_ratio") or 0.5)
    if buy_ratio < min_buy_ratio:
        return False, f"BUY_RATIO_BELOW_{min_buy_ratio}"

    buy_tax = float(cand.get("buy_tax") or 0)
    if buy_tax > max_buy_tax:
        return False, f"BUY_TAX_ABOVE_{max_buy_tax}"

    sell_tax = float(cand.get("sell_tax") or 0)
    if sell_tax > max_sell_tax:
        return False, f"SELL_TAX_ABOVE_{max_sell_tax}"

    if require_renounced_freeze and not cand.get("renounced_freeze"):
        return False, "FREEZE_NOT_RENOUNCED"

    return True, "PASSED"


def simulate_exit(
    cand: Dict[str, Any],
    stop_loss_pct: float = -0.035,
    rapid_cut_pnl: float = -0.020,
    take_profit_pct: float = 0.35,
) -> Dict[str, Any]:
    """
    Simulates exit logic over the price path (+1m, +5m, +15m).
    Returns the simulated exit reason and PnL at the exit point.
    """
    price_path = []
    for period, ret_key in [("1m", "return_1m"), ("5m", "return_5m"), ("15m", "return_15m")]:
        ret = cand.get(ret_key)
        if ret is not None:
            price_path.append((period, float(ret)))

    if not price_path:
        return {"exit_reason": "NO_PRICE_DATA", "exit_pnl_pct": 0.0, "exit_period": "N/A"}

    # Walk through price path and apply exit rules
    for period, ret in price_path:
        # Rapid momentum cut at +1m
        if period == "1m" and ret <= rapid_cut_pnl:
            return {"exit_reason": "RAPID_CUT", "exit_pnl_pct": ret, "exit_period": period}

        # Hard stop loss
        if ret <= stop_loss_pct:
            return {"exit_reason": "HARD_STOP", "exit_pnl_pct": stop_loss_pct, "exit_period": period}

        # Take profit
        if ret >= take_profit_pct:
            return {"exit_reason": "TAKE_PROFIT", "exit_pnl_pct": take_profit_pct, "exit_period": period}

    # If we reach end of path without triggering, use final return
    final_period, final_ret = price_path[-1]
    if final_ret > 0:
        return {"exit_reason": "GREEN_HOLD", "exit_pnl_pct": final_ret, "exit_period": final_period}
    else:
        return {"exit_reason": "STAGNATION_EXIT", "exit_pnl_pct": final_ret, "exit_period": final_period}


def run_replay(
    stop_loss: float = -0.035,
    rapid_cut: float = -0.020,
    take_profit: float = 0.35,
    min_liquidity: float = 45000.0,
    min_mcap: float = 40000.0,
    min_buy_ratio: float = 0.50,
    trade_size_sol: float = 0.35,
    limit: int = 5000,
) -> Dict[str, Any]:
    """Core replay engine: runs screening + exit over historical data."""
    candidates = load_candidates_with_returns(limit=limit)

    if not candidates:
        return {"error": "No historical candidates with forward returns found"}

    results = {
        "total_candidates": len(candidates),
        "gate_passed": 0,
        "gate_rejected": 0,
        "trades": [],
        "total_pnl_sol": 0.0,
        "wins": 0,
        "losses": 0,
        "exit_reasons": {},
    }

    for cand in candidates:
        # Replay gate
        passed, reason = simulate_gate_pass(
            cand,
            min_liquidity=min_liquidity,
            min_mcap=min_mcap,
            min_buy_ratio=min_buy_ratio,
        )

        if not passed:
            results["gate_rejected"] += 1
            continue

        results["gate_passed"] += 1

        # Replay exit
        exit_result = simulate_exit(
            cand,
            stop_loss_pct=stop_loss,
            rapid_cut_pnl=rapid_cut,
            take_profit_pct=take_profit,
        )

        pnl_pct = exit_result["exit_pnl_pct"]
        pnl_sol = round(trade_size_sol * pnl_pct, 6)

        trade = {
            "mint": cand.get("mint", ""),
            "symbol": cand.get("symbol", ""),
            "exit_reason": exit_result["exit_reason"],
            "exit_period": exit_result["exit_period"],
            "pnl_pct": round(pnl_pct, 4),
            "pnl_sol": pnl_sol,
        }
        results["trades"].append(trade)
        results["total_pnl_sol"] += pnl_sol

        if pnl_sol > 0:
            results["wins"] += 1
        else:
            results["losses"] += 1

        reason_key = exit_result["exit_reason"]
        if reason_key not in results["exit_reasons"]:
            results["exit_reasons"][reason_key] = {"count": 0, "pnl_sol": 0.0}
        results["exit_reasons"][reason_key]["count"] += 1
        results["exit_reasons"][reason_key]["pnl_sol"] += pnl_sol

    total_trades = results["wins"] + results["losses"]
    results["win_rate"] = round(results["wins"] / max(total_trades, 1), 4)
    results["avg_pnl_sol"] = round(results["total_pnl_sol"] / max(total_trades, 1), 6)
    results["profit_factor"] = _compute_profit_factor(results["trades"])
    results["total_pnl_sol"] = round(results["total_pnl_sol"], 6)

    return results


def _compute_profit_factor(trades: List[Dict[str, Any]]) -> float:
    """Gross winning PnL / abs(Gross losing PnL)."""
    gross_wins = sum(t["pnl_sol"] for t in trades if t["pnl_sol"] > 0)
    gross_losses = abs(sum(t["pnl_sol"] for t in trades if t["pnl_sol"] < 0))
    if gross_losses == 0:
        return float("inf") if gross_wins > 0 else 0.0
    return round(gross_wins / gross_losses, 3)


def main():
    parser = argparse.ArgumentParser(description="KIDA Citadel Replay Harness")
    parser.add_argument("--stop-loss", type=float, default=-0.035, help="Stop loss pct (negative)")
    parser.add_argument("--rapid-cut", type=float, default=-0.020, help="Rapid cut PnL threshold")
    parser.add_argument("--take-profit", type=float, default=0.35, help="Take profit pct")
    parser.add_argument("--min-liquidity", type=float, default=45000.0, help="Min pool liquidity USD")
    parser.add_argument("--min-mcap", type=float, default=40000.0, help="Min market cap")
    parser.add_argument("--min-buy-ratio", type=float, default=0.50, help="Min buy ratio")
    parser.add_argument("--trade-size", type=float, default=0.35, help="Trade size in SOL")
    parser.add_argument("--limit", type=int, default=5000, help="Max candidates to replay")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    init_db()

    results = run_replay(
        stop_loss=args.stop_loss,
        rapid_cut=args.rapid_cut,
        take_profit=args.take_profit,
        min_liquidity=args.min_liquidity,
        min_mcap=args.min_mcap,
        min_buy_ratio=args.min_buy_ratio,
        trade_size_sol=args.trade_size,
        limit=args.limit,
    )

    if args.json:
        # Strip individual trades for cleaner output
        output = {k: v for k, v in results.items() if k != "trades"}
        print(json.dumps(output, indent=2))
        return

    print("=" * 90)
    print("KIDA CITADEL // REPLAY HARNESS RESULTS")
    print("=" * 90)
    print(f"\n[CONFIG VARIANT]")
    print(f"  Stop Loss:     {args.stop_loss*100:+.1f}%")
    print(f"  Rapid Cut:     {args.rapid_cut*100:+.1f}%")
    print(f"  Take Profit:   {args.take_profit*100:+.1f}%")
    print(f"  Min Liquidity: ${args.min_liquidity:,.0f}")
    print(f"  Min Mcap:      ${args.min_mcap:,.0f}")
    print(f"  Min Buy Ratio: {args.min_buy_ratio:.2f}")
    print(f"  Trade Size:    {args.trade_size:.3f} SOL")

    if "error" in results:
        print(f"\n  ERROR: {results['error']}")
        return

    total_trades = results["wins"] + results["losses"]
    print(f"\n[SUMMARY]")
    print(f"  Total Candidates:    {results['total_candidates']}")
    print(f"  Gate Passed:         {results['gate_passed']}")
    print(f"  Gate Rejected:       {results['gate_rejected']}")
    print(f"  Simulated Trades:    {total_trades}")
    print(f"  Win Rate:            {results['win_rate']*100:.1f}%")
    print(f"  Total PnL:           {results['total_pnl_sol']:+.4f} SOL")
    print(f"  Avg PnL per Trade:   {results['avg_pnl_sol']:+.6f} SOL")
    print(f"  Profit Factor:       {results['profit_factor']:.3f}x")

    print(f"\n[EXIT REASON BREAKDOWN]")
    print(f"  {'Exit Reason':<25} | {'Count':<6} | {'Total PnL (SOL)':<15}")
    print("-" * 55)
    for reason, stats in sorted(results["exit_reasons"].items(), key=lambda x: -x[1]["count"]):
        print(f"  {reason:<25} | {stats['count']:<6} | {stats['pnl_sol']:>+12.4f}")

    print("\n" + "=" * 90)


if __name__ == "__main__":
    main()
