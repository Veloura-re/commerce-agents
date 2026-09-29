#!/usr/bin/env python3
"""
KIDA Citadel - Performance Dashboard & Summary Report (Phase 8)
Computes and displays:
- Net expectancy per trade
- Profit factor net of fees
- Max drawdown (session)
- Win rate and average PnL by exit reason
- Gate rejection statistics
- Average slippage and latency
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kida_bot.core.db import get_connection, get_exit_report_data, get_gate_report_data, init_db


def compute_dashboard_metrics(db_path=None):
    """Computes all dashboard metrics from the trades and candidates tables."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # 1. Core trade metrics
        cursor.execute(
            """
            SELECT COUNT(id) as total_trades,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losses,
                   SUM(realized_pnl) as total_pnl_sol,
                   AVG(realized_pnl) as avg_pnl_sol,
                   AVG(realized_pnl_pct) as avg_pnl_pct,
                   SUM(fees_paid) as total_fees_sol,
                   AVG(fees_paid) as avg_fees_sol,
                   SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END) as gross_wins,
                   SUM(CASE WHEN realized_pnl < 0 THEN ABS(realized_pnl) ELSE 0 END) as gross_losses,
                   AVG(realized_slippage) as avg_slippage,
                   AVG(slippage_through_stop) as avg_slippage_through_stop,
                   AVG(order_latency_ms) as avg_latency_ms,
                   MIN(order_latency_ms) as min_latency_ms,
                   MAX(order_latency_ms) as max_latency_ms
            FROM trades
            """
        )
        row = cursor.fetchone()

        total = row["total_trades"] or 0
        wins = row["wins"] or 0
        losses = row["losses"] or 0
        total_pnl = row["total_pnl_sol"] or 0.0
        total_fees = row["total_fees_sol"] or 0.0
        gross_wins = row["gross_wins"] or 0.0
        gross_losses = row["gross_losses"] or 0.0

        win_rate = (wins / total) if total > 0 else 0.0
        avg_pnl = (total_pnl / total) if total > 0 else 0.0
        net_pnl = total_pnl - total_fees
        net_expectancy = (net_pnl / total) if total > 0 else 0.0

        # Profit factor net of fees
        net_wins = gross_wins - (total_fees / 2.0)  # approximate fee split
        net_losses = gross_losses + (total_fees / 2.0)
        profit_factor_gross = (gross_wins / gross_losses) if gross_losses > 0 else 0.0
        profit_factor_net = (net_wins / net_losses) if net_losses > 0 else 0.0

        # 2. Max drawdown calculation (sequential equity curve)
        cursor.execute(
            """
            SELECT realized_pnl, exit_ts
            FROM trades
            ORDER BY exit_ts ASC
            """
        )
        trade_rows = cursor.fetchall()

        max_drawdown_sol = 0.0
        max_drawdown_pct = 0.0
        peak_equity = 0.0
        cumulative = 0.0

        for tr in trade_rows:
            cumulative += (tr["realized_pnl"] or 0.0)
            if cumulative > peak_equity:
                peak_equity = cumulative
            dd = peak_equity - cumulative
            if dd > max_drawdown_sol:
                max_drawdown_sol = dd
            if peak_equity > 0:
                dd_pct = dd / peak_equity
                if dd_pct > max_drawdown_pct:
                    max_drawdown_pct = dd_pct

        # 3. Candidate screening stats
        cursor.execute(
            """
            SELECT COUNT(id) as total_candidates,
                   SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) as passed,
                   SUM(CASE WHEN passed = 0 THEN 1 ELSE 0 END) as rejected
            FROM candidates
            """
        )
        cand_row = cursor.fetchone()

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "total_pnl_sol_gross": round(total_pnl, 6),
        "total_fees_sol": round(total_fees, 6),
        "total_pnl_sol_net": round(net_pnl, 6),
        "avg_pnl_sol": round(avg_pnl, 6),
        "net_expectancy_per_trade": round(net_expectancy, 6),
        "profit_factor_gross": round(profit_factor_gross, 3),
        "profit_factor_net": round(profit_factor_net, 3),
        "gross_winning_sol": round(gross_wins, 6),
        "gross_losing_sol": round(gross_losses, 6),
        "max_drawdown_sol": round(max_drawdown_sol, 6),
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "avg_slippage": round(row["avg_slippage"] or 0.0, 6),
        "avg_slippage_through_stop": round(row["avg_slippage_through_stop"] or 0.0, 6),
        "avg_latency_ms": round(row["avg_latency_ms"] or 0.0, 1),
        "min_latency_ms": round(row["min_latency_ms"] or 0.0, 1),
        "max_latency_ms": round(row["max_latency_ms"] or 0.0, 1),
        "total_candidates": cand_row["total_candidates"] or 0,
        "candidates_passed": cand_row["passed"] or 0,
        "candidates_rejected": cand_row["rejected"] or 0,
    }


def format_pct(val):
    if val is None:
        return "N/A"
    return f"{val * 100:.2f}%"


def main():
    init_db()
    m = compute_dashboard_metrics()
    exit_data = get_exit_report_data()
    gate_data = get_gate_report_data()

    print("=" * 90)
    print("KIDA CITADEL // PERFORMANCE DASHBOARD & SUMMARY REPORT")
    print("=" * 90)

    # Core Metrics
    print("\n[CORE PERFORMANCE METRICS]")
    print(f"  Total Settled Trades:          {m['total_trades']}")
    print(f"  Win / Loss:                    {m['wins']}W / {m['losses']}L")
    print(f"  Win Rate:                      {m['win_rate']*100:.1f}%")
    print(f"  Net Expectancy Per Trade:       {m['net_expectancy_per_trade']:+.6f} SOL")
    print(f"  Profit Factor (Gross):         {m['profit_factor_gross']:.3f}x")
    print(f"  Profit Factor (Net of Fees):   {m['profit_factor_net']:.3f}x")

    # PnL
    print("\n[PROFIT & LOSS]")
    print(f"  Total PnL (Gross):             {m['total_pnl_sol_gross']:+.6f} SOL")
    print(f"  Total Fees Paid:               {m['total_fees_sol']:.6f} SOL")
    print(f"  Total PnL (Net):               {m['total_pnl_sol_net']:+.6f} SOL")
    print(f"  Gross Winning:                 {m['gross_winning_sol']:+.6f} SOL")
    print(f"  Gross Losing:                  -{m['gross_losing_sol']:.6f} SOL")

    # Drawdown
    print("\n[DRAWDOWN]")
    print(f"  Max Drawdown (SOL):            {m['max_drawdown_sol']:.6f} SOL")
    print(f"  Max Drawdown (%):              {m['max_drawdown_pct']*100:.2f}%")

    # Execution Quality
    print("\n[EXECUTION QUALITY]")
    print(f"  Avg Slippage:                  {m['avg_slippage']*100:.3f}%")
    print(f"  Avg Slippage Through Stop:     {m['avg_slippage_through_stop']*100:.3f}%")
    print(f"  Avg Order Latency:             {m['avg_latency_ms']:.1f} ms")
    print(f"  Min / Max Latency:             {m['min_latency_ms']:.1f} ms / {m['max_latency_ms']:.1f} ms")

    # Exit Reason Breakdown
    print("\n[WIN RATE & AVG PNL BY EXIT REASON]")
    print("-" * 80)
    print(f"  {'Exit Reason':<30} | {'Count':<6} | {'Win %':<7} | {'Avg PnL %':<10} | {'Total SOL':<10}")
    print("-" * 80)

    if exit_data:
        for r in exit_data:
            reason = r.get("exit_reason", "UNKNOWN")[:28]
            total = r.get("total_trades", 0)
            win_count = r.get("win_count", 0)
            wr = (win_count / total * 100) if total > 0 else 0.0
            avg_pct = (r.get("avg_pnl_pct", 0.0) or 0.0) * 100
            total_sol = r.get("total_pnl_sol", 0.0) or 0.0
            print(f"  {reason:<30} | {total:<6} | {wr:>5.1f}% | {avg_pct:>+8.2f}% | {total_sol:>+9.4f}")
    else:
        print("  No settled trades logged yet.")

    # Gate Rejection Stats
    print("\n[GATE REJECTION STATISTICS]")
    print("-" * 80)
    print(f"  Total Candidates Screened:     {m['total_candidates']}")
    print(f"  Passed Gates:                  {m['candidates_passed']}")
    print(f"  Rejected:                      {m['candidates_rejected']}")
    if m["total_candidates"] > 0:
        pass_rate = m["candidates_passed"] / m["total_candidates"] * 100
        print(f"  Pass Rate:                     {pass_rate:.1f}%")

    if gate_data and gate_data.get("gates"):
        print(f"\n  {'Gate':<35} | {'Rejected':<8} | {'Avg +5m Return':<15}")
        print("  " + "-" * 65)
        for gate_name, stats in gate_data["gates"].items():
            avg_5m = format_pct(stats.get("avg_return_5m"))
            print(f"  {gate_name:<35} | {stats['count']:<8} | {avg_5m}")

    print("\n" + "=" * 90)


if __name__ == "__main__":
    main()
