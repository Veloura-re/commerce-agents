import unittest
import time
import tempfile
from pathlib import Path

from kida_bot.core.safety_checks import (
    init_blacklist_table,
    is_blacklisted,
    add_to_blacklist,
    record_loss_and_blacklist,
    validate_freeze_and_lp,
    simulate_sell_order,
    validate_snapshot_freshness,
)
from kida_bot.core.guardrails import (
    ProvenanceRegistry,
    TokenProvenance,
    StagedSwap,
    SwapKind,
    check_swap_guardrails,
)


class TestPhase3SafetyChecks(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "kida_test.db"
        # Reset cache for clean test isolation
        import kida_bot.core.safety_checks as sc
        sc._BLACKLIST_CACHE = set()
        sc._BLACKLIST_LOADED = False
        init_blacklist_table(self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_freeze_and_lp_burn_rules(self):
        # Freeze not renounced -> reject
        ok, reason = validate_freeze_and_lp(renounced_freeze=False, burn_ratio=1.0)
        self.assertFalse(ok)
        self.assertIn("FREEZE_AUTHORITY_NOT_RENOUNCED", reason)

        # LP burn below 95% -> reject
        ok, reason = validate_freeze_and_lp(renounced_freeze=True, burn_ratio=0.80)
        self.assertFalse(ok)
        self.assertIn("LP_BURN_OR_LOCK_INSUFFICIENT", reason)

        # Both valid -> pass
        ok, reason = validate_freeze_and_lp(renounced_freeze=True, burn_ratio=0.98)
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")

    def test_snapshot_freshness(self):
        now = time.time()
        # Fresh snapshot (5s ago) -> pass
        ok, reason = validate_snapshot_freshness(now - 5.0, max_age_seconds=60.0)
        self.assertTrue(ok)

        # Stale snapshot (95s ago) -> reject
        ok, reason = validate_snapshot_freshness(now - 95.0, max_age_seconds=60.0)
        self.assertFalse(ok)
        self.assertIn("STALE_SNAPSHOT_DATA", reason)

    def test_sell_simulation(self):
        # Successful quote within bounds
        def mock_good_quote(in_token, out_token, amount):
            return {"price_impact": 0.015, "implied_tax": 0.0}

        ok, msg, _ = simulate_sell_order("Token111111111111111111111111111111111111", 0.5, quote_provider=mock_good_quote)
        self.assertTrue(ok)
        self.assertIn("PASSED", msg)

        # Excessive impact quote (> 5%)
        def mock_high_impact_quote(in_token, out_token, amount):
            return {"price_impact": 0.08, "implied_tax": 0.0}

        ok, msg, _ = simulate_sell_order("Token111111111111111111111111111111111111", 0.5, quote_provider=mock_high_impact_quote)
        self.assertFalse(ok)
        self.assertIn("SELL_SIMULATION_IMPACT_EXCESSIVE", msg)

        # Honeypot / Routing failure
        def mock_failing_quote(in_token, out_token, amount):
            return {"error": "INSUFFICIENT_LIQUIDITY_FOR_SWAP"}

        ok, msg, _ = simulate_sell_order("Token111111111111111111111111111111111111", 0.5, quote_provider=mock_failing_quote)
        self.assertFalse(ok)
        self.assertIn("SELL_SIMULATION_FAILED", msg)

        # Excessive exit tax (> 8%)
        def mock_tax_quote(in_token, out_token, amount):
            return {"price_impact": 0.01, "implied_tax": 0.12}

        ok, msg, _ = simulate_sell_order("Token111111111111111111111111111111111111", 0.5, quote_provider=mock_tax_quote)
        self.assertFalse(ok)
        self.assertIn("SELL_SIMULATION_TAX_EXCESSIVE", msg)

    def test_blacklist_persistence_and_lookup(self):
        token_mint = "TokenBad111111111111111111111111111111111111"
        deployer = "DevScammer111111111111111111111111111111111"
        bundler = "BundlerCluster11111111111111111111111111111"

        hit, _ = is_blacklisted(token_mint, db_path=self.db_path)
        self.assertFalse(hit)

        # Record loss and blacklist
        record_loss_and_blacklist(
            mint=token_mint,
            deployer=deployer,
            bundler=bundler,
            loss_sol=0.08,
            reason="Bleeder stopped out",
            db_path=self.db_path
        )

        # Token should be blacklisted
        hit_t, reason_t = is_blacklisted(token_mint, db_path=self.db_path)
        self.assertTrue(hit_t)
        self.assertIn("BLACKLISTED", reason_t)

        # Deployer should be blacklisted
        hit_d, reason_d = is_blacklisted(deployer, db_path=self.db_path)
        self.assertTrue(hit_d)
        self.assertIn("DEPLOYER", reason_d)

        # Bundler should be blacklisted
        hit_b, reason_b = is_blacklisted(bundler, db_path=self.db_path)
        self.assertTrue(hit_b)
        self.assertIn("BUNDLER", reason_b)

    def test_guardrails_blocks_blacklisted_or_unrenounced_token(self):
        registry = ProvenanceRegistry()
        mint = "TestToken11111111111111111111111111111111111"
        registry.register_screened_candidate({
            "address": mint,
            "symbol": "BAD",
            "market_cap": 80000.0,
            "liquidity": 35000.0,
            "volume_24h": 50000.0,
            "buy_ratio": 0.60
        })

        # Test unrenounced freeze rejected by guardrails
        swap = StagedSwap(
            stage_id="s1",
            kind=SwapKind.BUY_FRESH,
            address=mint,
            symbol="BAD",
            size_sol=0.3,
            context={"renounced_freeze": False, "burn_ratio": 1.0}
        )
        violations = check_swap_guardrails(
            staged=swap,
            portfolio_context={"positions": [], "total_exposure": 0.0},
            provenance_registry=registry,
            live_heuristics={},
            bot_state={}
        )
        self.assertTrue(any("FREEZE_AUTHORITY_NOT_RENOUNCED" in v for v in violations))


if __name__ == "__main__":
    unittest.main()
