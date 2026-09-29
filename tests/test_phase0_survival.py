import unittest
import pathlib
import time
import json
import os
import tempfile
import yaml

from kida_bot.config import (
    _load_and_validate_config,
    CONFIG_YAML_PATH,
    PAPER_MODE,
    DAILY_LOSS_CAP_SOL,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DRAWDOWN_PCT,
    TOTAL_BANKROLL_SOL
)
from kida_bot.core.killswitch import check_killswitch, get_killswitch_status
from kida_bot.core.watchdog import write_heartbeat, read_heartbeat, check_watchdog
from kida_bot.core.guardrails import check_swap_guardrails, StagedSwap, SwapKind, SwapStatus, ProvenanceRegistry

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

class TestPhase0Survival(unittest.TestCase):

    def setUp(self):
        self.halt_file = PROJECT_ROOT / "HALT"
        if self.halt_file.exists():
            self.halt_file.unlink()

    def tearDown(self):
        if self.halt_file.exists():
            self.halt_file.unlink()

    def test_config_loads_and_validates(self):
        cfg = _load_and_validate_config(CONFIG_YAML_PATH)
        self.assertIsInstance(cfg, dict)
        self.assertTrue(cfg["mode"]["paper_mode"])
        self.assertGreater(cfg["capital"]["total_bankroll_sol"], 0)
        self.assertLess(cfg["stops"]["stop_loss_pct"], 0)
        self.assertGreater(cfg["gates"]["min_pool_liquidity_usd"], 0)

    def test_config_validation_fails_on_bad_values(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
            bad_cfg = {
                "mode": {"paper_mode": "not_a_bool"},
                "capital": {"total_bankroll_sol": -5},
                "killswitch": {"daily_loss_cap_sol": 0},
                "stops": {"stop_loss_pct": 0.05} # positive stop loss is invalid
            }
            yaml.dump(bad_cfg, tf)
            tf_path = pathlib.Path(tf.name)

        try:
            with self.assertRaises(ValueError):
                _load_and_validate_config(tf_path)
        finally:
            if tf_path.exists():
                tf_path.unlink()

    def test_killswitch_manual_halt_file(self):
        self.assertFalse(self.halt_file.exists())
        tripped, reason = check_killswitch(bot_state={})
        self.assertFalse(tripped)

        # Create HALT file
        self.halt_file.write_text("EMERGENCY TEST HALT", encoding="utf-8")
        tripped, reason = check_killswitch(bot_state={})
        self.assertTrue(tripped)
        self.assertIn("MANUAL_HALT_ENGAGED", reason)

        # Clean up
        self.halt_file.unlink()
        tripped, reason = check_killswitch(bot_state={})
        self.assertFalse(tripped)

    def test_killswitch_daily_loss_limit(self):
        # Normal state
        bot_state = {"total_realized_pnl_sol": -0.50}
        tripped, reason = check_killswitch(bot_state=bot_state)
        self.assertFalse(tripped)

        # Hit daily loss limit
        bot_state = {"total_realized_pnl_sol": -(DAILY_LOSS_CAP_SOL + 0.01)}
        tripped, reason = check_killswitch(bot_state=bot_state)
        self.assertTrue(tripped)
        self.assertIn("DAILY_LOSS_CAP_TRIPPED", reason)

    def test_killswitch_consecutive_losses(self):
        # 3 losses - below threshold
        bot_state = {
            "total_realized_pnl_sol": -0.10,
            "closed_trades": [
                {"symbol": "T1", "pnl_sol": -0.02},
                {"symbol": "T2", "pnl_sol": -0.03},
                {"symbol": "T3", "pnl_sol": -0.05}
            ]
        }
        tripped, reason = check_killswitch(bot_state=bot_state)
        self.assertFalse(tripped)

        # Hit max consecutive losses (e.g. 6)
        bot_state["closed_trades"] = [
            {"symbol": f"T{i}", "pnl_sol": -0.01} for i in range(MAX_CONSECUTIVE_LOSSES)
        ]
        tripped, reason = check_killswitch(bot_state=bot_state)
        self.assertTrue(tripped)
        self.assertIn("CONSECUTIVE_LOSS_CIRCUIT_BREAKER", reason)

    def test_killswitch_max_drawdown(self):
        # Peak equity = 10 SOL. Floating loss 1.6 SOL, realized loss 0.2 SOL (Total equity 8.2 SOL -> 18% drawdown > 15% limit)
        bot_state = {
            "peak_session_equity_sol": 10.0,
            "total_realized_pnl_sol": -0.20,
            "active_positions": [{"symbol": "TOKEN", "pnl": -0.50, "size_sol": 3.20}]
        }
        tripped, reason = check_killswitch(bot_state=bot_state)
        self.assertTrue(tripped)
        self.assertIn("MAX_DRAWDOWN_TRIPPED", reason)

    def test_watchdog_heartbeat_emission_and_staleness(self):
        write_heartbeat(cycle_count=42, active_positions_count=2, mode="SHADOW")
        hb = read_heartbeat()
        self.assertTrue(hb["exists"])
        self.assertFalse(hb["is_stalled"])
        self.assertEqual(hb["data"]["cycle_count"], 42)
        self.assertEqual(hb["data"]["active_positions_count"], 2)

        # Verify check_watchdog helper
        healthy, msg = check_watchdog()
        self.assertTrue(healthy)

    def test_guardrails_blocks_buy_when_killswitch_tripped(self):
        self.halt_file.write_text("HALT", encoding="utf-8")
        staged = StagedSwap(
            stage_id="test-stage-01",
            kind=SwapKind.BUY_FRESH,
            address="So11111111111111111111111111111111111111112",
            symbol="TEST",
            size_sol=0.35,
            percent=100
        )
        prov = ProvenanceRegistry()
        prov.register_held_position("So11111111111111111111111111111111111111112", "TEST")

        violations = check_swap_guardrails(
            staged=staged,
            portfolio_context={"positions": [], "total_exposure": 0.0},
            provenance_registry=prov,
            live_heuristics={"max_bundler_rate": 0.1, "max_dev_hold_rate": 0.1},
            bot_state={}
        )
        self.assertTrue(any("KILLSWITCH_TRIPPED" in v for v in violations))
        self.halt_file.unlink()

if __name__ == "__main__":
    unittest.main()
