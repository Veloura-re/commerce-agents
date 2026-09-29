"""
Phase 1 Test Suite: SQLite Logging, Forward Returns, and Reporting Tools
Verifies data/kida.db schema, candidate logging, forward returns tracking,
settled trade audits, tools/gate_report.py, and tools/exit_report.py.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kida_bot.core.db import (
    get_connection,
    get_exit_report_data,
    get_gate_report_data,
    get_pending_forward_returns,
    init_db,
    log_candidate,
    log_candidates_batch,
    log_trade,
    update_forward_return,
)
from kida_bot.core.forward_tracker import run_tracker_cycle


class TestPhase1Logging(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_kida.db"
        init_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_schema_initialization(self):
        """Verify all three tables and indices are initialized correctly."""
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row["name"] for row in cursor.fetchall()}
            self.assertIn("candidates", tables)
            self.assertIn("forward_returns", tables)
            self.assertIn("trades", tables)

    def test_candidate_logging_with_14_features(self):
        """Verify candidate logging records all 14 features and sets up forward_returns."""
        cand = {
            "ts": time.time(),
            "mint": "TokenABC1111111111111111111111111111111111",
            "symbol": "ABC",
            "price": 0.00045,
            "liquidity": 65000.0,
            "mcap": 120000.0,
            "vol_1h": 85000.0,
            "age_min": 24.5,
            "chg_1h": 0.185,
            "chg_5m": 0.042,
            "buy_ratio": 0.62,
            "turnover": 0.71,
            "honeypot": 0,
            "renounced_mint": 1,
            "renounced_freeze": 1,
            "buy_tax": 0.0,
            "sell_tax": 0.0,
            "bundler": 0.08,
            "dev_hold": 0.03,
            "top10": 0.19,
            "smart_degen": 3,
            "renowned": 1,
            "sm_confluence": 4,
            "dex": "RAY-CPMM",
            "per_gate_pass_fail": {"gate1": True, "gate2": True, "gate3": True, "gate4": True},
            "passed": 1,
            "first_rejection_reason": None,
        }

        cand_id = log_candidate(cand, db_path=self.db_path)
        self.assertGreater(cand_id, 0)

        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM candidates WHERE id = ?", (cand_id,))
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["symbol"], "ABC")
            self.assertEqual(row["passed"], 1)
            self.assertAlmostEqual(row["price"], 0.00045)
            self.assertEqual(row["renounced_freeze"], 1)

            # Check forward_returns initialized
            cursor.execute("SELECT * FROM forward_returns WHERE candidate_id = ?", (cand_id,))
            fwd = cursor.fetchone()
            self.assertIsNotNone(fwd)
            self.assertAlmostEqual(fwd["initial_price"], 0.00045)
            self.assertEqual(fwd["completed"], 0)

    def test_rejected_candidate_logging(self):
        """Verify rejected candidates are logged with exact gate failure reason."""
        cand = {
            "ts": time.time(),
            "mint": "TokenBad2222222222222222222222222222222222",
            "symbol": "BAD",
            "price": 0.00010,
            "liquidity": 12000.0,
            "mcap": 25000.0,
            "passed": 0,
            "first_rejection_reason": "REJECT LIQUIDITY: $12,000 < $45,000",
            "per_gate_pass_fail": {"gate1": False, "gate2": False, "gate3": False, "gate4": False},
        }

        cand_id = log_candidate(cand, db_path=self.db_path)
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM candidates WHERE id = ?", (cand_id,))
            row = cursor.fetchone()
            self.assertEqual(row["passed"], 0)
            self.assertIn("REJECT LIQUIDITY", row["first_rejection_reason"])

    def test_forward_returns_update(self):
        """Verify forward returns math for +1m, +5m, and +15m."""
        cand = {
            "ts": time.time(),
            "mint": "TokenWin3333333333333333333333333333333333",
            "symbol": "WIN",
            "price": 0.00100,
            "passed": 1,
        }
        cand_id = log_candidate(cand, db_path=self.db_path)

        # Update +1m price to 0.00110 (+10%)
        update_forward_return(cand_id, "1m", 0.00110, 0.00100, db_path=self.db_path)
        # Update +5m price to 0.00125 (+25%)
        update_forward_return(cand_id, "5m", 0.00125, 0.00100, db_path=self.db_path)
        # Update +15m price to 0.00150 (+50%) and complete
        update_forward_return(cand_id, "15m", 0.00150, 0.00100, completed=True, db_path=self.db_path)

        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM forward_returns WHERE candidate_id = ?", (cand_id,))
            fwd = cursor.fetchone()
            self.assertAlmostEqual(fwd["return_1m"], 0.10, places=4)
            self.assertAlmostEqual(fwd["return_5m"], 0.25, places=4)
            self.assertAlmostEqual(fwd["return_15m"], 0.50, places=4)
            self.assertEqual(fwd["completed"], 1)

    def test_forward_tracker_cycle(self):
        """Verify the background tracker cycle queries prices and updates intervals."""
        past_ts = time.time() - 70.0  # 70 seconds ago (> 1m)
        cand = {
            "ts": past_ts,
            "mint": "TokenTracker4444444444444444444444444444444",
            "symbol": "TRK",
            "price": 0.00200,
            "passed": 1,
        }
        cand_id = log_candidate(cand, db_path=self.db_path)

        # Mock price function returning 0.00220
        def mock_price_fn(mint: str) -> float:
            return 0.00220

        updated = run_tracker_cycle(mock_price_fn, db_path=self.db_path)
        self.assertEqual(updated, 1)

        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM forward_returns WHERE candidate_id = ?", (cand_id,))
            fwd = cursor.fetchone()
            self.assertAlmostEqual(fwd["price_1m"], 0.00220)
            self.assertAlmostEqual(fwd["return_1m"], 0.10, places=4)

    def test_trade_audit_logging(self):
        """Verify trade logging captures all required fields."""
        trade = {
            "trade_id": "trade_101_SOLPUMP",
            "mint": "TokenTrade55555555555555555555555555555555",
            "symbol": "SOLPUMP",
            "size": 0.50,
            "entry_ts": time.time() - 300,
            "exit_ts": time.time(),
            "entry_price": 0.00010,
            "exit_price": 0.00012,
            "exit_reason": "PARTIAL_TP_TIER1 (+10% hit)",
            "realized_pnl": 0.10,
            "realized_pnl_pct": 0.20,
            "fees_paid": 0.005,
            "priority_fee": 0.002,
            "quoted_slippage": 0.01,
            "realized_slippage": 0.008,
            "order_latency_ms": 142.5,
        }

        trade_id = log_trade(trade, db_path=self.db_path)
        self.assertGreater(trade_id, 0)

        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades WHERE trade_id = ?", ("trade_101_SOLPUMP",))
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["symbol"], "SOLPUMP")
            self.assertAlmostEqual(row["realized_pnl"], 0.10)
            self.assertAlmostEqual(row["order_latency_ms"], 142.5)

    def test_gate_report_and_exit_report_data(self):
        """Verify report generation queries aggregate data properly."""
        # 1. Log candidates
        p_cand = {
            "ts": time.time(),
            "mint": "PassedToken",
            "symbol": "PASS",
            "price": 0.001,
            "passed": 1,
        }
        pid = log_candidate(p_cand, db_path=self.db_path)
        update_forward_return(pid, "5m", 0.0015, 0.001, db_path=self.db_path)

        r_cand = {
            "ts": time.time(),
            "mint": "RejectedToken",
            "symbol": "REJ",
            "price": 0.002,
            "passed": 0,
            "first_rejection_reason": "REJECT 避雷：未放弃增发权",
        }
        rid = log_candidate(r_cand, db_path=self.db_path)
        update_forward_return(rid, "5m", 0.0010, 0.002, db_path=self.db_path)

        gate_data = get_gate_report_data(db_path=self.db_path)
        self.assertEqual(gate_data["passed"]["count"], 1)
        self.assertAlmostEqual(gate_data["passed"]["avg_return_5m"], 0.50, places=2)
        gate1_stats = gate_data["gates"]["Gate 1 (Hard Safety & Momentum)"]
        self.assertEqual(gate1_stats["count"], 1)
        self.assertAlmostEqual(gate1_stats["avg_return_5m"], -0.50, places=2)

        # 2. Log trades across categories
        log_trade({
            "trade_id": "t1", "mint": "M1", "symbol": "S1", "size": 0.5,
            "entry_ts": time.time() - 30, "exit_ts": time.time(),
            "entry_price": 1.0, "exit_price": 0.98,
            "exit_reason": "RAPID_CUT_25S (-2.0% cut)",
            "realized_pnl": -0.01, "realized_pnl_pct": -0.02, "fees_paid": 0.005
        }, db_path=self.db_path)

        log_trade({
            "trade_id": "t2", "mint": "M2", "symbol": "S2", "size": 0.5,
            "entry_ts": time.time() - 300, "exit_ts": time.time(),
            "entry_price": 1.0, "exit_price": 1.22,
            "exit_reason": "PARTIAL_TP_TIER2 (+20% hit)",
            "realized_pnl": 0.11, "realized_pnl_pct": 0.22, "fees_paid": 0.005
        }, db_path=self.db_path)

        exit_data = get_exit_report_data(db_path=self.db_path)
        self.assertEqual(len(exit_data), 2)


if __name__ == "__main__":
    unittest.main()
