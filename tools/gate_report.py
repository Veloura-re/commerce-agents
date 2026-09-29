#!/usr/bin/env python3
"""
KIDA Citadel - Gate Performance & Winner Rejection Report
Evaluates whether screening gates are rejecting winning tokens or correctly cutting risk.
Compares forward returns (+1m, +5m, +15m) for candidates passed vs rejected per gate.
"""

import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kida_bot.core.db import get_gate_report_data, init_db


def format_pct(val: float | None) -> str:
    if val is None:
        return "N/A"
    return f"{val * 100:+.2f}%"


def main():
    init_db()
    data = get_gate_report_data()

    passed = data["passed"]
    gates = data["gates"]

    print("=" * 80)
    print("KIDA CITADEL // SCREENING GATE PERFORMANCE & FORWARD RETURNS AUDIT")
    print("=" * 80)
    print(f"Total Passed Candidates : {passed['count']}")
    print(f"Passed Avg Return (+1m)  : {format_pct(passed['avg_return_1m'])}")
    print(f"Passed Avg Return (+5m)  : {format_pct(passed['avg_return_5m'])}")
    print(f"Passed Avg Return (+15m) : {format_pct(passed['avg_return_15m'])}")
    print("-" * 80)
    print(f"{'Gate / Filter':<35} | {'Rejected':<8} | {'Avg +1m':<10} | {'Avg +5m':<10} | {'Avg +15m':<10}")
    print("-" * 80)

    total_rejected = 0
    for gate_name, stats in gates.items():
        total_rejected += stats["count"]
        print(
            f"{gate_name:<35} | "
            f"{stats['count']:<8} | "
            f"{format_pct(stats['avg_return_1m']):<10} | "
            f"{format_pct(stats['avg_return_5m']):<10} | "
            f"{format_pct(stats['avg_return_15m']):<10}"
        )

    print("-" * 80)
    print(f"Total Rejected Candidates: {total_rejected}")

    # Comparative Edge Analysis
    print("\n[QUANTITATIVE EDGE ANALYSIS]")
    for gate_name, stats in gates.items():
        if stats["count"] == 0 or stats["avg_return_5m"] is None:
            continue
        p5 = passed["avg_return_5m"] or 0.0
        r5 = stats["avg_return_5m"]
        diff = p5 - r5
        verdict = "EDGE CONFIRMED (Filtered out negative / lower performers)" if diff > 0 else "WARNING: Potential alpha leak (Rejected tokens outgained survivors)"
        print(f"- {gate_name}: Delta at +5m: {diff * 100:+.2f}% -> {verdict}")

    print("=" * 80)


if __name__ == "__main__":
    main()
