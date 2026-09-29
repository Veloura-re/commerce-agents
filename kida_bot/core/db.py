"""
KIDA Citadel Database Module
SQLite data store managing candidates, forward returns, and settled trades.
All magic values and configs calibrated from real logged records.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("kida_citadel.db")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "kida.db"


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    target_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """Initializes tables and indices for candidates, forward returns, and trades."""
    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                mint TEXT NOT NULL,
                symbol TEXT,
                price REAL NOT NULL,
                liquidity REAL,
                mcap REAL,
                vol_1h REAL,
                age_min REAL,
                chg_1h REAL,
                chg_5m REAL,
                buy_ratio REAL,
                turnover REAL,
                honeypot INTEGER,
                renounced_mint INTEGER,
                renounced_freeze INTEGER,
                buy_tax REAL,
                sell_tax REAL,
                bundler REAL,
                dev_hold REAL,
                top10 REAL,
                smart_degen INTEGER,
                renowned INTEGER,
                sm_confluence INTEGER,
                dex TEXT,
                per_gate_pass_fail TEXT,
                passed INTEGER NOT NULL,
                first_rejection_reason TEXT,
                raw_features_json TEXT,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_candidates_mint ON candidates(mint);
            CREATE INDEX IF NOT EXISTS idx_candidates_ts ON candidates(ts);
            CREATE INDEX IF NOT EXISTS idx_candidates_passed ON candidates(passed);

            CREATE TABLE IF NOT EXISTS forward_returns (
                candidate_id INTEGER PRIMARY KEY,
                mint TEXT NOT NULL,
                initial_price REAL NOT NULL,
                initial_ts REAL NOT NULL,
                price_1m REAL,
                return_1m REAL,
                price_5m REAL,
                return_5m REAL,
                price_15m REAL,
                return_15m REAL,
                completed INTEGER DEFAULT 0,
                updated_at REAL,
                FOREIGN KEY(candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_fwd_completed ON forward_returns(completed);
            CREATE INDEX IF NOT EXISTS idx_fwd_mint ON forward_returns(mint);

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT UNIQUE,
                mint TEXT NOT NULL,
                symbol TEXT,
                size REAL NOT NULL,
                entry_ts REAL NOT NULL,
                exit_ts REAL NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                exit_reason TEXT NOT NULL,
                realized_pnl REAL NOT NULL,
                realized_pnl_pct REAL NOT NULL,
                fees_paid REAL DEFAULT 0.0,
                priority_fee REAL DEFAULT 0.0,
                quoted_slippage REAL DEFAULT 0.0,
                realized_slippage REAL DEFAULT 0.0,
                slippage_through_stop REAL DEFAULT 0.0,
                order_latency_ms REAL DEFAULT 0.0,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_trades_mint ON trades(mint);
            CREATE INDEX IF NOT EXISTS idx_trades_exit_reason ON trades(exit_reason);
            CREATE INDEX IF NOT EXISTS idx_trades_exit_ts ON trades(exit_ts);
            """
        )
        # Migration for existing databases
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN slippage_through_stop REAL DEFAULT 0.0")
        except sqlite3.OperationalError:
            pass
        conn.commit()


def log_candidate(cand: Dict[str, Any], db_path: Optional[Path] = None) -> int:
    """Logs a single candidate and registers initial forward returns tracking entry."""
    ids = log_candidates_batch([cand], db_path=db_path)
    return ids[0] if ids else -1


def log_candidates_batch(cands: List[Dict[str, Any]], db_path: Optional[Path] = None) -> List[int]:
    """Logs a batch of screened candidates in a single transaction."""
    if not cands:
        return []

    inserted_ids: List[int] = []
    now = time.time()

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        for cand in cands:
            ts = cand.get("ts", now)
            mint = cand.get("mint") or cand.get("address") or ""
            symbol = cand.get("symbol") or cand.get("symbol_safe") or ""
            price = float(cand.get("price") or 0.0)
            liquidity = float(cand.get("liquidity") or 0.0)
            mcap = float(cand.get("mcap") or 0.0)
            vol_1h = float(cand.get("vol_1h") or 0.0)
            age_min = float(cand.get("age_min") or 0.0)
            chg_1h = float(cand.get("chg_1h") or 0.0)
            chg_5m = float(cand.get("chg_5m") or 0.0)
            buy_ratio = float(cand.get("buy_ratio") or 0.5)
            turnover = float(cand.get("turnover") or 0.0)
            honeypot = 1 if cand.get("honeypot") else 0
            renounced_mint = 1 if cand.get("renounced_mint") else 0
            renounced_freeze = 1 if cand.get("renounced_freeze") else 0
            buy_tax = float(cand.get("buy_tax") or 0.0)
            sell_tax = float(cand.get("sell_tax") or 0.0)
            bundler = float(cand.get("bundler") or 0.0)
            dev_hold = float(cand.get("dev_hold") or 0.0)
            top10 = float(cand.get("top10") or 0.0)
            smart_degen = int(cand.get("smart_degen") or 0)
            renowned = int(cand.get("renowned") or 0)
            sm_confluence = int(cand.get("sm_confluence") or 0)
            dex = str(cand.get("dex") or "RAY-CPMM")

            per_gate = cand.get("per_gate_pass_fail")
            if isinstance(per_gate, (dict, list)):
                per_gate_json = json.dumps(per_gate)
            else:
                per_gate_json = str(per_gate or "{}")

            passed = 1 if cand.get("passed") else 0
            rejection = cand.get("first_rejection_reason")
            if rejection is not None:
                rejection = str(rejection)

            raw_feat = cand.get("raw_features_json")
            if raw_feat is None and "features" in cand:
                raw_feat = json.dumps(cand["features"])
            elif isinstance(raw_feat, (dict, list)):
                raw_feat = json.dumps(raw_feat)

            cursor.execute(
                """
                INSERT INTO candidates (
                    ts, mint, symbol, price, liquidity, mcap, vol_1h, age_min,
                    chg_1h, chg_5m, buy_ratio, turnover, honeypot, renounced_mint,
                    renounced_freeze, buy_tax, sell_tax, bundler, dev_hold, top10,
                    smart_degen, renowned, sm_confluence, dex, per_gate_pass_fail,
                    passed, first_rejection_reason, raw_features_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts, mint, symbol, price, liquidity, mcap, vol_1h, age_min,
                    chg_1h, chg_5m, buy_ratio, turnover, honeypot, renounced_mint,
                    renounced_freeze, buy_tax, sell_tax, bundler, dev_hold, top10,
                    smart_degen, renowned, sm_confluence, dex, per_gate_json,
                    passed, rejection, raw_feat
                )
            )
            cand_id = cursor.lastrowid
            inserted_ids.append(cand_id)

            # Initialize forward_returns entry if initial price is valid
            if price > 0.0 and mint:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO forward_returns (
                        candidate_id, mint, initial_price, initial_ts, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (cand_id, mint, price, ts, now)
                )

        conn.commit()
    return inserted_ids


def log_trade(trade: Dict[str, Any], db_path: Optional[Path] = None) -> int:
    """Logs a completed or partial trade into the trades audit table."""
    now = time.time()
    trade_id = trade.get("trade_id") or f"trade_{int(now * 1000)}_{trade.get('mint', 'unknown')[:8]}"
    mint = trade.get("mint") or trade.get("address") or ""
    symbol = trade.get("symbol") or ""
    size = float(trade.get("size") or trade.get("size_sol") or 0.0)
    entry_ts = float(trade.get("entry_ts") or (now - 60.0))
    exit_ts = float(trade.get("exit_ts") or now)
    entry_price = float(trade.get("entry_price") or 0.0)
    exit_price = float(trade.get("exit_price") or 0.0)
    exit_reason = str(trade.get("exit_reason") or trade.get("reason") or "UNKNOWN")
    realized_pnl = float(trade.get("realized_pnl") or trade.get("pnl_sol") or 0.0)
    realized_pnl_pct = float(trade.get("realized_pnl_pct") or trade.get("pnl_pct") or 0.0)
    fees_paid = float(trade.get("fees_paid") or trade.get("fee_sol") or 0.0)
    priority_fee = float(trade.get("priority_fee") or 0.0)
    quoted_slippage = float(trade.get("quoted_slippage") or 0.01)
    realized_slippage = float(trade.get("realized_slippage") or 0.0)
    slippage_through_stop = float(trade.get("slippage_through_stop") or 0.0)
    order_latency_ms = float(trade.get("order_latency_ms") or 0.0)

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO trades (
                trade_id, mint, symbol, size, entry_ts, exit_ts,
                entry_price, exit_price, exit_reason, realized_pnl, realized_pnl_pct,
                fees_paid, priority_fee, quoted_slippage, realized_slippage,
                slippage_through_stop, order_latency_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id, mint, symbol, size, entry_ts, exit_ts,
                entry_price, exit_price, exit_reason, realized_pnl, realized_pnl_pct,
                fees_paid, priority_fee, quoted_slippage, realized_slippage,
                slippage_through_stop, order_latency_ms
            )
        )
        conn.commit()
        return cursor.lastrowid


def update_forward_return(
    candidate_id: int,
    period: str,
    price: float,
    initial_price: float,
    completed: bool = False,
    db_path: Optional[Path] = None
) -> None:
    """Updates the forward return for a specific interval (+1m, +5m, +15m)."""
    if initial_price <= 0.0 or price <= 0.0:
        ret = 0.0
    else:
        ret = (price - initial_price) / initial_price

    now = time.time()
    valid_periods = {"1m": ("price_1m", "return_1m"),
                     "5m": ("price_5m", "return_5m"),
                     "15m": ("price_15m", "return_15m")}

    if period not in valid_periods:
        return

    price_col, ret_col = valid_periods[period]
    completed_val = 1 if completed else 0

    with get_connection(db_path) as conn:
        conn.execute(
            f"""
            UPDATE forward_returns
            SET {price_col} = ?, {ret_col} = ?, completed = max(completed, ?), updated_at = ?
            WHERE candidate_id = ?
            """,
            (price, ret, completed_val, now, candidate_id)
        )
        conn.commit()


def mark_forward_return_completed(candidate_id: int, db_path: Optional[Path] = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE forward_returns SET completed = 1, updated_at = ? WHERE candidate_id = ?",
            (time.time(), candidate_id)
        )
        conn.commit()


def get_pending_forward_returns(limit: int = 200, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Returns candidate records that still need forward returns evaluated."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT candidate_id, mint, initial_price, initial_ts,
                   price_1m, return_1m, price_5m, return_5m, price_15m, return_15m,
                   completed
            FROM forward_returns
            WHERE completed = 0
            ORDER BY initial_ts ASC
            LIMIT ?
            """,
            (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]


def get_gate_report_data(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Computes rejection statistics and forward returns comparison across gates.
    Answers: Are we rejecting winners vs those passed?
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        # Fetch passed stats
        cursor.execute(
            """
            SELECT COUNT(c.id) as count,
                   AVG(f.return_1m) as avg_ret_1m,
                   AVG(f.return_5m) as avg_ret_5m,
                   AVG(f.return_15m) as avg_ret_15m
            FROM candidates c
            LEFT JOIN forward_returns f ON c.id = f.candidate_id
            WHERE c.passed = 1
            """
        )
        passed_row = cursor.fetchone()
        passed_stats = {
            "count": passed_row["count"] or 0,
            "avg_return_1m": passed_row["avg_ret_1m"],
            "avg_return_5m": passed_row["avg_ret_5m"],
            "avg_return_15m": passed_row["avg_ret_15m"],
        }

        # Fetch all rejected candidates with their first rejection reason
        cursor.execute(
            """
            SELECT c.id, c.first_rejection_reason, c.per_gate_pass_fail,
                   f.return_1m, f.return_5m, f.return_15m
            FROM candidates c
            LEFT JOIN forward_returns f ON c.id = f.candidate_id
            WHERE c.passed = 0
            """
        )
        rejected_rows = cursor.fetchall()

        # Classify by Gate 1, Gate 2, Gate 3, Gate 4
        gate_buckets: Dict[str, Dict[str, Any]] = {
            "Gate 1 (Hard Safety & Momentum)": {"count": 0, "ret_1m": [], "ret_5m": [], "ret_15m": []},
            "Gate 2 (Smart Money Consensus)": {"count": 0, "ret_1m": [], "ret_5m": [], "ret_15m": []},
            "Gate 3 (Dev Score & Rank Pool)": {"count": 0, "ret_1m": [], "ret_5m": [], "ret_15m": []},
            "Gate 4 (LLM Conviction)": {"count": 0, "ret_1m": [], "ret_5m": [], "ret_15m": []},
            "Other / Pre-Filter": {"count": 0, "ret_1m": [], "ret_5m": [], "ret_15m": []},
        }

        for row in rejected_rows:
            reason = (row["first_rejection_reason"] or "").lower()
            bucket = "Other / Pre-Filter"
            if any(k in reason for k in ["避雷", "tax", "honeypot", "renounced", "liquidity", "mcap", "动能", "买盘不足", "quality", "gate 1", "overbought"]):
                bucket = "Gate 1 (Hard Safety & Momentum)"
            elif any(k in reason for k in ["共识", "smart", "kol", "gate 2"]):
                bucket = "Gate 2 (Smart Money Consensus)"
            elif any(k in reason for k in ["dev", "排序", "gate 3"]):
                bucket = "Gate 3 (Dev Score & Rank Pool)"
            elif any(k in reason for k in ["llm", "置信度", "conviction", "gate 4"]):
                bucket = "Gate 4 (LLM Conviction)"

            gate_buckets[bucket]["count"] += 1
            if row["return_1m"] is not None:
                gate_buckets[bucket]["ret_1m"].append(row["return_1m"])
            if row["return_5m"] is not None:
                gate_buckets[bucket]["ret_5m"].append(row["return_5m"])
            if row["return_15m"] is not None:
                gate_buckets[bucket]["ret_15m"].append(row["return_15m"])

        gate_stats = {}
        for k, v in gate_buckets.items():
            count = v["count"]
            avg_1m = sum(v["ret_1m"]) / len(v["ret_1m"]) if v["ret_1m"] else None
            avg_5m = sum(v["ret_5m"]) / len(v["ret_5m"]) if v["ret_5m"] else None
            avg_15m = sum(v["ret_15m"]) / len(v["ret_15m"]) if v["ret_15m"] else None
            gate_stats[k] = {
                "count": count,
                "avg_return_1m": avg_1m,
                "avg_return_5m": avg_5m,
                "avg_return_15m": avg_15m,
            }

        return {"passed": passed_stats, "gates": gate_stats}


def get_exit_report_data(db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Groups settled trades by exit reason, showing count, PnL, win rate, and fees."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT exit_reason,
                   COUNT(id) as total_trades,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as win_count,
                   SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as loss_count,
                   SUM(realized_pnl) as total_pnl_sol,
                   AVG(realized_pnl) as avg_pnl_sol,
                   AVG(realized_pnl_pct) as avg_pnl_pct,
                   SUM(fees_paid) as total_fees_sol,
                   AVG(realized_slippage) as avg_slippage,
                   AVG(order_latency_ms) as avg_latency_ms
            FROM trades
            GROUP BY exit_reason
            ORDER BY total_trades DESC
            """
        )
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
