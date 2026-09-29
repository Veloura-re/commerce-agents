import unittest
import time
import tempfile
from pathlib import Path

from kida_bot.core.volatility import (
    VolatilityTracker,
    calculate_slippage_through_stop,
)
from kida_bot.core.db import init_db, log_trade, get_connection


class TestPhase4VolatilityExits(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "kida_test.db"
        init_db(self.db_path)
        self.tracker = VolatilityTracker(window_seconds=60.0)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_short_window_range_calculation(self):
        mint = "So11111111111111111111111111111111111111112"
        now = time.time()
        # Add price samples over 30s
        self.tracker.add_price_sample(mint, 100.0, ts=now - 20)
        self.tracker.add_price_sample(mint, 104.0, ts=now - 10)
        self.tracker.add_price_sample(mint, 98.0, ts=now - 5)

        # Range should be (104 - 98) / 98 = 6 / 98 ≈ 0.06122 (6.12%)
        range_pct = self.tracker.get_short_window_range(mint, cur_price=101.0)
        self.assertAlmostEqual(range_pct, 6.0 / 98.0, places=4)

    def test_sample_eviction_outside_window(self):
        mint = "MintOld111111111111111111111111111111111111"
        now = time.time()
        # Old sample 120s ago (outside 60s window)
        self.tracker.add_price_sample(mint, 50.0, ts=now - 120)
        # Recent samples
        self.tracker.add_price_sample(mint, 100.0, ts=now - 10)
        self.tracker.add_price_sample(mint, 102.0, ts=now)

        # 50.0 should be evicted, so min is 100.0, max is 102.0
        range_pct = self.tracker.get_short_window_range(mint, cur_price=102.0)
        self.assertAlmostEqual(range_pct, 2.0 / 100.0, places=4)

    def test_volatility_stop_computation_clamping(self):
        mint = "TokenTest11111111111111111111111111111111111"
        now = time.time()
        # Moderate range: 100 to 102 (2%)
        self.tracker.add_price_sample(mint, 100.0, ts=now - 10)
        self.tracker.add_price_sample(mint, 102.0, ts=now)

        # With k=1.5 and range=0.02, raw stop distance = 1.5 * 0.02 = 0.030 (3.0%)
        stops = self.tracker.compute_volatility_stops(
            mint=mint,
            cur_price=101.0,
            round_trip_cost_pct=0.012,
            k=1.5
        )
        self.assertEqual(stops["stop_distance_pct"], 0.030)
        self.assertEqual(stops["stop_loss_pct"], -0.030)
        self.assertLessEqual(stops["rapid_cut_pnl"], -0.012)
        self.assertLessEqual(stops["bleeder_cut_pnl"], -0.010)

        # Extreme high volatility: 100 to 150 (50%) -> should clamp to MAX_STOP_DISTANCE_PCT (0.050)
        self.tracker.add_price_sample(mint, 150.0, ts=now)
        extreme_stops = self.tracker.compute_volatility_stops(
            mint=mint,
            cur_price=120.0,
            round_trip_cost_pct=0.012,
            k=1.5
        )
        self.assertEqual(extreme_stops["stop_distance_pct"], 0.050)
        self.assertEqual(extreme_stops["stop_loss_pct"], -0.050)

    def test_calculate_slippage_through_stop(self):
        # Stop price was 100.0, filled at 98.0 -> 2.0% slippage through stop
        slip = calculate_slippage_through_stop(100.0, 98.0)
        self.assertAlmostEqual(slip, 0.020, places=4)

        # Stop price was 100.0, filled at 100.0 -> 0.0% slippage
        slip_zero = calculate_slippage_through_stop(100.0, 100.0)
        self.assertEqual(slip_zero, 0.0)

        # Filled above stop (positive execution) -> 0.0% slippage past stop
        slip_pos = calculate_slippage_through_stop(100.0, 100.5)
        self.assertEqual(slip_pos, 0.0)

    def test_feed_latency_blind_flagging(self):
        mint = "TokenFeed1111111111111111111111111111111111"
        # Normal latency 400ms
        self.tracker.record_feed_latency(mint, 400.0)
        is_blind, lat = self.tracker.is_feed_blind(mint, threshold_ms=3000.0)
        self.assertFalse(is_blind)
        self.assertEqual(lat, 400.0)

        # Lagging latency 4200ms
        self.tracker.record_feed_latency(mint, 4200.0)
        is_blind, lat = self.tracker.is_feed_blind(mint, threshold_ms=3000.0)
        self.assertTrue(is_blind)
        self.assertEqual(lat, 4200.0)

    def test_trades_table_persists_slippage_through_stop(self):
        trade_id = "test_vol_trade_001"
        log_trade({
            "trade_id": trade_id,
            "mint": "TokenSlip111111111111111111111111111111111",
            "symbol": "SLIP",
            "size": 0.5,
            "entry_ts": time.time() - 40,
            "exit_ts": time.time(),
            "entry_price": 0.010,
            "exit_price": 0.0096,
            "exit_reason": "VOLATILITY_STOP (-3.5%, Slip: 0.82%)",
            "realized_pnl": -0.02,
            "realized_pnl_pct": -0.04,
            "fees_paid": 0.005,
            "slippage_through_stop": 0.0082,
            "order_latency_ms": 35.0
        }, db_path=self.db_path)

        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT slippage_through_stop, exit_reason FROM trades WHERE trade_id = ?", (trade_id,))
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertAlmostEqual(row["slippage_through_stop"], 0.0082, places=4)
            self.assertIn("VOLATILITY_STOP", row["exit_reason"])


if __name__ == "__main__":
    unittest.main()
