#!/usr/bin/env python3
"""
KIDA Citadel - Weight & Feature Importance Report (Phase 7)
Compares candidate features vs forward returns from the forward_returns table
to determine which features actually predict positive returns (the honest way
to learn which features matter, independent of the backprop loop).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kida_bot.core.db import get_connection, init_db
from kida_bot.strategies.backprop import FEATURE_KEYS, WEIGHT_LOG_FILE, load_weights


def compute_feature_return_correlations(db_path=None):
    """
    Computes average forward returns for candidates bucketed by each feature
    (above vs below median) to identify which features have predictive power.
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT c.smart_degen as sm, c.liquidity, c.chg_5m, c.buy_ratio,
                   f.return_1m, f.return_5m, f.return_15m
            FROM candidates c
            INNER JOIN forward_returns f ON c.id = f.candidate_id
            WHERE f.completed = 1 AND f.return_5m IS NOT NULL
            """
        )
        rows = cursor.fetchall()

    if not rows:
        return None

    features = {
        "smart_money": [r["sm"] or 0 for r in rows],
        "liquidity": [r["liquidity"] or 0 for r in rows],
        "momentum_5m": [r["chg_5m"] or 0 for r in rows],
        "buy_ratio": [r["buy_ratio"] or 0.5 for r in rows],
    }
    returns_5m = [r["return_5m"] or 0.0 for r in rows]
    returns_15m = [r["return_15m"] or 0.0 for r in rows]

    results = {}
    for feat_name, values in features.items():
        median_val = sorted(values)[len(values) // 2]

        high_indices = [i for i, v in enumerate(values) if v >= median_val]
        low_indices = [i for i, v in enumerate(values) if v < median_val]

        high_avg_5m = sum(returns_5m[i] for i in high_indices) / max(len(high_indices), 1)
        low_avg_5m = sum(returns_5m[i] for i in low_indices) / max(len(low_indices), 1)
        high_avg_15m = sum(returns_15m[i] for i in high_indices) / max(len(high_indices), 1)
        low_avg_15m = sum(returns_15m[i] for i in low_indices) / max(len(low_indices), 1)

        results[feat_name] = {
            "median": median_val,
            "above_median_count": len(high_indices),
            "below_median_count": len(low_indices),
            "above_median_avg_return_5m": round(high_avg_5m, 6),
            "below_median_avg_return_5m": round(low_avg_5m, 6),
            "above_median_avg_return_15m": round(high_avg_15m, 6),
            "below_median_avg_return_15m": round(low_avg_15m, 6),
            "edge_5m": round(high_avg_5m - low_avg_5m, 6),
            "edge_15m": round(high_avg_15m - low_avg_15m, 6),
        }

    return {"total_samples": len(rows), "features": results}


def print_weight_history():
    """Reads the JSONL weight change log and prints the last 20 entries."""
    if not WEIGHT_LOG_FILE.exists():
        print("No weight change history found yet.")
        return

    lines = WEIGHT_LOG_FILE.read_text().strip().split("\n")
    import json

    print(f"\n{'Trade ID':<30} | {'PnL':>8} | {'Signal':>7} | {'SM':>7} | {'LIQ':>7} | {'MOM':>7} | {'BR':>7}")
    print("-" * 95)

    for line in lines[-20:]:
        try:
            entry = json.loads(line)
            tid = entry.get("trade_id", "?")[:28]
            pnl = entry.get("pnl", 0.0)
            sig = entry.get("reward_signal", 0.0)
            d = entry.get("deltas", {})
            print(
                f"{tid:<30} | {pnl:>+7.4f} | {sig:>+6.4f} | "
                f"{d.get('w_smart_money', 0):>+6.4f} | {d.get('w_liquidity', 0):>+6.4f} | "
                f"{d.get('w_momentum', 0):>+6.4f} | {d.get('w_buy_ratio', 0):>+6.4f}"
            )
        except Exception:
            continue


def main():
    init_db()

    print("=" * 90)
    print("KIDA CITADEL // WEIGHT & FEATURE IMPORTANCE ANALYSIS REPORT")
    print("=" * 90)

    # 1. Current weights
    weights = load_weights()
    print("\n[CURRENT WEIGHTS]")
    for k in FEATURE_KEYS:
        print(f"  {k}: {weights.get(k, 0.0):.4f}")

    # 2. Feature vs forward return analysis
    data = compute_feature_return_correlations()
    if data:
        print(f"\n[FEATURE vs FORWARD RETURN ANALYSIS] ({data['total_samples']} completed samples)")
        print("-" * 90)
        print(
            f"  {'Feature':<18} | {'Median':<10} | {'Above N':<8} | {'Below N':<8} | "
            f"{'Above +5m':<10} | {'Below +5m':<10} | {'Edge 5m':<10} | {'Edge 15m':<10}"
        )
        print("-" * 90)

        for feat, stats in data["features"].items():
            med_str = f"{stats['median']:.4f}" if isinstance(stats["median"], float) else str(stats["median"])
            print(
                f"  {feat:<18} | {med_str:<10} | {stats['above_median_count']:<8} | "
                f"{stats['below_median_count']:<8} | "
                f"{stats['above_median_avg_return_5m']*100:>+8.2f}% | "
                f"{stats['below_median_avg_return_5m']*100:>+8.2f}% | "
                f"{stats['edge_5m']*100:>+8.2f}% | "
                f"{stats['edge_15m']*100:>+8.2f}%"
            )

        # Recommendation
        print("\n[FEATURE RANKING BY PREDICTIVE EDGE AT +5m]")
        ranked = sorted(data["features"].items(), key=lambda x: -x[1]["edge_5m"])
        for rank, (feat, stats) in enumerate(ranked, 1):
            verdict = "PREDICTIVE" if stats["edge_5m"] > 0.001 else "WEAK/NOISE"
            print(f"  #{rank} {feat}: Edge {stats['edge_5m']*100:+.3f}% -> {verdict}")
    else:
        print("\n[FEATURE ANALYSIS] Insufficient data. Need completed forward_returns rows.")

    # 3. Weight change history
    print("\n[WEIGHT CHANGE HISTORY (last 20)]")
    print_weight_history()

    print("\n" + "=" * 90)


if __name__ == "__main__":
    main()
