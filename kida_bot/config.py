import urllib.request
import urllib.error
import time
import json
import logging
import random
import requests
import pathlib
import hashlib
import sys
import shutil
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum, auto
from collections import deque
from tenacity import retry, stop_after_attempt, wait_exponential

HERE = pathlib.Path(__file__).resolve().parent.parent
LOG_FILE = HERE / "aitrader" / "outputs" / "rotation_session_1h.log"
AUDIT_FILE = HERE / "aitrader" / "outputs" / "session_audit.json"
OUTPUTS_DIR = HERE / "aitrader" / "outputs"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("ContinuousRotationEngine")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
ch = logging.StreamHandler()
ch.setFormatter(formatter)
logger.addHandler(ch)
fh = logging.FileHandler(str(LOG_FILE), mode="w", encoding="utf-8")
fh.setFormatter(formatter)
logger.addHandler(fh)

API_BASE = "http://127.0.0.1:8000"
CHAIN = "sol"
FAST_MONITOR_INTERVAL_SECONDS = 3.0
SCREEN_INTERVAL_SECONDS = 20.0
TOTAL_BANKROLL_USD = 1000.0
CAPITAL_UTILIZATION_PCT = 0.90
MAX_ACTIVE_EXPOSURE_SOL = 15.00
DEFAULT_TRADE_SIZE_SOL = 0.200
MIN_TRADE_SIZE_SOL = 0.200
MAX_TRADE_SIZE_SOL = 0.200
MAX_POSITIONS = 4
SESSION_DURATION_SECONDS = 31536000

TAKE_PROFIT_PCT = 0.350
STOP_LOSS_PCT = -0.040
REENTRY_STOP_LOSS_PCT = -0.025
MAX_SLIPPAGE_TOLERANCE = 0.050
STAGNATION_TIMEOUT_SECONDS = 15 * 60
EMERGENCY_LIQUIDITY_DROP_THRESHOLD = 0.15
MOONBAG_TRAILING_STOP_PULLBACK = 0.15

TIER1_TRIGGER_PCT   = 0.050      # +5.0% gain: Locks Break-Even floor
TIER1_LOCK_PCT      = 0.015      # +1.5% locked
TIER2_TRIGGER_PCT   = 0.100      # +10.0% gain: Banks 50% partial profit
TIER2_LOCK_PCT      = 0.050      # +5.0% locked
TIER3_TRIGGER_PCT   = 0.200      # +20.0% gain: Banks 30% partial profit
TIER3_LOCK_PCT      = 0.120      # +12.0% locked

MAX_REENTRIES_PER_TOKEN = 3

state = {
    "session_start_time": time.time(),
    "cycle_count": 0,
    "tokens_scanned": 0,
    "total_trades": 0,
    "closed_trades": [],
    "total_realized_pnl_sol": 0.0,
    "entry_timestamps": {},
    "entry_liquidities": {},
    "peak_liquidity": {},
    "position_sizes": {},
    "partial_tp_taken": set(),
    "break_even_locked": set(),
    "is_reentry": {},
    "reentry_count": {},
    "moonbag_status": set(),
    "moonbag_ath": {},
    "held_addresses": set()
}

def save_audit_state(get_positions_func):
    try:
        positions = get_positions_func()
        unrealized_sol = sum(p.get("pnl", 0) * p.get("size_sol", DEFAULT_TRADE_SIZE_SOL) for p in positions)
        wins = [t for t in state["closed_trades"] if t.get("pnl_sol", 0) > 0]
        losses = [t for t in state["closed_trades"] if t.get("pnl_sol", 0) < 0]
        
        data = {
            "session_start": state["session_start_time"],
            "elapsed_seconds": time.time() - state["session_start_time"],
            "cycles": state["cycle_count"],
            "realized_pnl_sol": state["total_realized_pnl_sol"],
            "unrealized_pnl_sol": unrealized_sol,
            "win_rate": (len(wins) / len(state["closed_trades"])) if state["closed_trades"] else 0.0
        }
        with open(AUDIT_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save audit state: {e}")
