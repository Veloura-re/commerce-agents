"""
KIDA Citadel Real Cost Model Module
Measures realized round-trip friction (DEX swap fees, priority fees/tips, price impact in and out).
Calculates dynamic break-even thresholds for Green Vault and Tier 1 floors, and audits gross vs net PnL.
"""

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from kida_bot.config import (
    EXPECTED_FIRST_TARGET_PCT,
    GREEN_VAULT_MARGIN,
    MAX_COST_TO_FIRST_TARGET_RATIO,
    MAX_PRICE_IMPACT_PCT,
    PRIORITY_FEE_SOL,
    SWAP_FEE_PCT,
    TIER1_FLOOR_MARGIN,
)
from kida_bot.core.db import get_connection

logger = logging.getLogger("kida_citadel.cost_model")


def measure_realized_costs(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Measures historical realized round-trip trading costs directly from the SQLite trades table.
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(id) as total_trades,
                   AVG(size) as avg_size_sol,
                   AVG(fees_paid) as avg_fees_sol,
                   AVG(priority_fee) as avg_priority_fee_sol,
                   AVG(realized_slippage) as avg_slippage,
                   AVG(order_latency_ms) as avg_latency_ms
            FROM trades
            """
        )
        row = cursor.fetchone()
        cnt = row["total_trades"] or 0
        avg_size = row["avg_size_sol"] or 0.35
        avg_fees = row["avg_fees_sol"] or (2 * (avg_size * SWAP_FEE_PCT) + 2 * PRIORITY_FEE_SOL)
        avg_prio = row["avg_priority_fee_sol"] or PRIORITY_FEE_SOL
        avg_slip = row["avg_slippage"] or 0.005

        avg_rt_cost_sol = avg_fees + (2 * avg_slip * avg_size)
        avg_rt_cost_pct = (avg_rt_cost_sol / avg_size) if avg_size > 0 else 0.02

        return {
            "sample_size": cnt,
            "avg_trade_size_sol": round(avg_size, 4),
            "avg_fees_sol": round(avg_fees, 6),
            "avg_priority_fee_sol": round(avg_prio, 6),
            "avg_slippage_pct": round(avg_slip, 4),
            "realized_round_trip_cost_sol": round(avg_rt_cost_sol, 6),
            "realized_round_trip_cost_pct": round(avg_rt_cost_pct, 4),
        }


def estimate_round_trip_cost(
    mint: str,
    size_sol: float,
    pool_liquidity_usd: float = 0.0,
    quote_provider: Optional[Callable[[str, str, float], Dict[str, Any]]] = None,
    sol_price_usd: float = 140.0
) -> Dict[str, Any]:
    """
    Computes expected round-trip execution cost: swap fees + priority fees + price impact in & out.
    Uses live quoting when available, or constant product AMM depth modeling.
    """
    clean_size = max(0.01, float(size_sol))

    # 1. Base swap fees (two legs: in and out)
    entry_swap_fee_sol = clean_size * SWAP_FEE_PCT
    exit_swap_fee_sol = clean_size * SWAP_FEE_PCT
    swap_fees_sol = entry_swap_fee_sol + exit_swap_fee_sol

    # 2. Priority fees / tips (two legs: in and out)
    priority_fees_sol = 2.0 * PRIORITY_FEE_SOL

    # 3. Price impact estimation
    entry_impact_pct = 0.004  # baseline 0.4%
    exit_impact_pct = 0.004

    if quote_provider is not None:
        try:
            buy_q = quote_provider("So11111111111111111111111111111111111111112", mint, clean_size)
            sell_q = quote_provider(mint, "So11111111111111111111111111111111111111112", clean_size)
            if buy_q and "price_impact" in buy_q:
                entry_impact_pct = max(0.001, float(buy_q["price_impact"]))
            if sell_q and "price_impact" in sell_q:
                exit_impact_pct = max(0.001, float(sell_q["price_impact"]))
        except Exception as e:
            logger.debug(f"Live quote provider error: {e}")
    elif pool_liquidity_usd > 0.0:
        # Constant product AMM impact approximation: size_usd / (2 * pool_liquidity_usd)
        trade_usd = clean_size * sol_price_usd
        est_impact = trade_usd / max(pool_liquidity_usd, 1000.0)
        entry_impact_pct = max(0.001, min(0.10, est_impact))
        exit_impact_pct = entry_impact_pct

    impact_cost_sol = clean_size * (entry_impact_pct + exit_impact_pct)
    total_cost_sol = swap_fees_sol + priority_fees_sol + impact_cost_sol
    round_trip_cost_pct = total_cost_sol / clean_size

    return {
        "mint": mint,
        "size_sol": clean_size,
        "entry_swap_fee_sol": round(entry_swap_fee_sol, 6),
        "exit_swap_fee_sol": round(exit_swap_fee_sol, 6),
        "total_swap_fees_sol": round(swap_fees_sol, 6),
        "priority_fees_sol": round(priority_fees_sol, 6),
        "entry_impact_pct": round(entry_impact_pct, 4),
        "exit_impact_pct": round(exit_impact_pct, 4),
        "impact_cost_sol": round(impact_cost_sol, 6),
        "total_cost_sol": round(total_cost_sol, 6),
        "round_trip_cost_pct": round(round_trip_cost_pct, 4),
    }


def validate_entry_cost(
    mint: str,
    size_sol: float,
    pool_liquidity_usd: float = 0.0,
    first_target_pct: float = EXPECTED_FIRST_TARGET_PCT,
    quote_provider: Optional[Callable[[str, str, float], Dict[str, Any]]] = None
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Pre-trade safety gate: Rejects entries where round-trip cost exceeds a config
    fraction of the expected first target or price impact is excessive.
    """
    est = estimate_round_trip_cost(
        mint=mint,
        size_sol=size_sol,
        pool_liquidity_usd=pool_liquidity_usd,
        quote_provider=quote_provider
    )

    cost_pct = est["round_trip_cost_pct"]
    max_allowed_cost_pct = first_target_pct * MAX_COST_TO_FIRST_TARGET_RATIO

    if cost_pct > max_allowed_cost_pct:
        reason = (
            f"EXCESSIVE_ROUND_TRIP_COST: Estimated friction {cost_pct * 100:.2f}% "
            f"exceeds {MAX_COST_TO_FIRST_TARGET_RATIO * 100:.0f}% ceiling of target {first_target_pct * 100:.1f}% "
            f"(Max allowed: {max_allowed_cost_pct * 100:.2f}%)"
        )
        return False, reason, est

    if est["entry_impact_pct"] > MAX_PRICE_IMPACT_PCT or est["exit_impact_pct"] > MAX_PRICE_IMPACT_PCT:
        reason = (
            f"EXCESSIVE_PRICE_IMPACT: Estimated leg impact (In: {est['entry_impact_pct']*100:.2f}%, "
            f"Out: {est['exit_impact_pct']*100:.2f}%) exceeds safety limit ({MAX_PRICE_IMPACT_PCT*100:.1f}%)"
        )
        return False, reason, est

    return True, "COST_CHECK_PASSED", est


def compute_dynamic_floors(round_trip_cost_pct: float) -> Dict[str, float]:
    """
    Computes dynamic profit triggers based on real measured round-trip cost + margin:
    - Green Vault trigger >= cost + GREEN_VAULT_MARGIN (not hardcoded at +0.6%)
    - Tier 1 floor >= cost + TIER1_FLOOR_MARGIN (not hardcoded at +2.0%)
    """
    c = max(0.005, float(round_trip_cost_pct))
    green_vault = round(c + GREEN_VAULT_MARGIN, 4)
    tier1_floor = round(c + TIER1_FLOOR_MARGIN, 4)

    return {
        "round_trip_cost_pct": round(c, 4),
        "green_vault_trigger_pct": green_vault,
        "tier1_floor_pct": tier1_floor,
    }


def calculate_net_pnl(
    size_sol: float,
    gross_pnl_pct: float,
    fees_paid_sol: Optional[float] = None
) -> Dict[str, float]:
    """
    Computes gross vs net-of-fees PnL for auditing and dashboards.
    """
    size = max(0.001, float(size_sol))
    gross_sol = round(size * gross_pnl_pct, 6)
    fees = fees_paid_sol if fees_paid_sol is not None else round(size * 0.008 + 2 * PRIORITY_FEE_SOL, 6)
    net_sol = round(gross_sol - fees, 6)
    net_pct = round(net_sol / size, 4)

    return {
        "gross_pnl_pct": round(gross_pnl_pct, 4),
        "gross_pnl_sol": gross_sol,
        "fees_paid_sol": round(fees, 6),
        "net_pnl_sol": net_sol,
        "net_pnl_pct": net_pct,
    }
