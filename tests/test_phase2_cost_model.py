"""
Phase 2 Test Suite: Real Cost Model, Dynamic Floors, and Fee Drag Accounting
Verifies round-trip friction estimation, pre-trade cost rejection,
dynamic Green Vault / Tier 1 triggers, gross vs net PnL reconciliation, and guardrails.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kida_bot.config import (
    GREEN_VAULT_MARGIN,
    MAX_COST_TO_FIRST_TARGET_RATIO,
    MAX_PRICE_IMPACT_PCT,
    PRIORITY_FEE_SOL,
    SWAP_FEE_PCT,
    TIER1_FLOOR_MARGIN,
)
from kida_bot.core.cost_model import (
    calculate_net_pnl,
    compute_dynamic_floors,
    estimate_round_trip_cost,
    measure_realized_costs,
    validate_entry_cost,
)
from kida_bot.core.guardrails import (
    ProvenanceRegistry,
    StagedSwap,
    SwapKind,
    check_swap_guardrails,
)


class TestPhase2CostModel(unittest.TestCase):
    def test_estimate_round_trip_cost_structure(self):
        """Verifies swap fees, priority fees, and price impact calculation."""
        size = 0.50
        est = estimate_round_trip_cost(
            mint="TokenTest1111111111111111111111111111111111",
            size_sol=size,
            pool_liquidity_usd=100000.0,
            sol_price_usd=140.0
        )

        self.assertAlmostEqual(est["size_sol"], 0.50)
        # 2 legs of swap fees = 2 * (0.50 * 0.0025) = 0.0025 SOL
        self.assertAlmostEqual(est["total_swap_fees_sol"], 2 * size * SWAP_FEE_PCT, places=6)
        # 2 legs of priority fees = 2 * 0.002 = 0.004 SOL
        self.assertAlmostEqual(est["priority_fees_sol"], 2 * PRIORITY_FEE_SOL, places=6)
        self.assertGreater(est["total_cost_sol"], 0.0)
        self.assertGreater(est["round_trip_cost_pct"], 0.0)

    def test_validate_entry_cost_thin_liquidity_rejection(self):
        """Rejects entry when pool liquidity is too thin, blowing out price impact."""
        # 0.5 SOL trade on tiny $1,200 pool -> huge price impact
        ok, reason, est = validate_entry_cost(
            mint="ThinPoolToken2222222222222222222222222222222",
            size_sol=0.50,
            pool_liquidity_usd=1200.0
        )
        self.assertFalse(ok)
        self.assertTrue("EXCESSIVE_PRICE_IMPACT" in reason or "EXCESSIVE_ROUND_TRIP_COST" in reason)

    def test_validate_entry_cost_deep_liquidity_approval(self):
        """Approves entry when pool liquidity is deep and fees are low."""
        ok, reason, est = validate_entry_cost(
            mint="DeepPoolToken3333333333333333333333333333333",
            size_sol=0.35,
            pool_liquidity_usd=150000.0,
            first_target_pct=0.08
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "COST_CHECK_PASSED")

    def test_dynamic_floors_calculation(self):
        """Ensures Green Vault and Tier 1 floors are at least (cost + margin)."""
        cost_pct = 0.015  # 1.5% round trip cost
        floors = compute_dynamic_floors(cost_pct)

        expected_vault = round(cost_pct + GREEN_VAULT_MARGIN, 4)
        expected_tier1 = round(cost_pct + TIER1_FLOOR_MARGIN, 4)

        self.assertAlmostEqual(floors["green_vault_trigger_pct"], expected_vault)
        self.assertAlmostEqual(floors["tier1_floor_pct"], expected_tier1)
        self.assertGreater(floors["green_vault_trigger_pct"], cost_pct)
        self.assertGreater(floors["tier1_floor_pct"], floors["green_vault_trigger_pct"])

    def test_gross_vs_net_pnl_reconciliation(self):
        """Verifies net PnL strictly accounts for all fees and drag."""
        size = 0.50
        gross_pnl_pct = 0.10  # +10% gross gain
        fees = 0.005  # 0.005 SOL paid in fees

        pnl = calculate_net_pnl(size, gross_pnl_pct, fees_paid_sol=fees)

        # Gross: 0.50 * 0.10 = +0.05 SOL
        self.assertAlmostEqual(pnl["gross_pnl_sol"], 0.05)
        # Net: 0.05 - 0.005 = +0.045 SOL
        self.assertAlmostEqual(pnl["net_pnl_sol"], 0.045)
        # Net %: 0.045 / 0.50 = +9.0%
        self.assertAlmostEqual(pnl["net_pnl_pct"], 0.09)

    def test_guardrails_block_excessive_cost(self):
        """Verifies that guardrail checks block swaps exceeding round-trip cost limits."""
        prov_reg = ProvenanceRegistry(ttl_seconds=3600)
        cand = {
            "address": "SuperIlliquidToken44444444444444444444444",
            "symbol": "ILLIQ",
            "liquidity": 1500.0,
            "mcap": 25000.0,
            "dev_hold": 0.02,
            "bundler": 0.02,
        }
        prov_reg.register_screened_candidate(cand)

        staged = StagedSwap(
            stage_id="stage_cost_test",
            kind=SwapKind.BUY_FRESH,
            address="SuperIlliquidToken44444444444444444444444",
            symbol="ILLIQ",
            size_sol=0.45,
            reason="FRESH_ENTRY"
        )

        violations = check_swap_guardrails(
            staged=staged,
            portfolio_context={"positions": [], "total_exposure": 0.0},
            provenance_registry=prov_reg,
            live_heuristics={},
            bot_state={"closed_trades": []}
        )

        has_cost_violation = any("EXCESSIVE" in v or "IMPACT" in v for v in violations)
        self.assertTrue(has_cost_violation)


if __name__ == "__main__":
    unittest.main()
