import os
import re

with open("autonomous_trader.py", "r") as f:
    content = f.read()

# Let's extract imports and set up the files
imports = """import time
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
"""

config_content = imports + """
HERE = pathlib.Path(__file__).resolve().parent.parent
LOG_FILE = HERE / "aitrader" / "outputs" / "rotation_session_1h.log"
AUDIT_FILE = HERE / "aitrader" / "outputs" / "session_audit.json"

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
CAPITAL_UTILIZATION_PCT = 0.50
MAX_ACTIVE_EXPOSURE_SOL = 3.60
DEFAULT_TRADE_SIZE_SOL = 0.700
MIN_TRADE_SIZE_SOL = 0.350
MAX_TRADE_SIZE_SOL = 2.000
MAX_POSITIONS = 5
SESSION_DURATION_SECONDS = 31536000

TAKE_PROFIT_PCT = 0.350
STOP_LOSS_PCT = -0.040
REENTRY_STOP_LOSS_PCT = -0.025
MAX_SLIPPAGE_TOLERANCE = 0.020
STAGNATION_TIMEOUT_SECONDS = 15 * 60
EMERGENCY_LIQUIDITY_DROP_THRESHOLD = 0.15
MOONBAG_TRAILING_STOP_PULLBACK = 0.15

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
"""

network_content = imports + """
from kida_bot.config import logger, API_BASE

# Mock webhook for future integration
def send_webhook_alert(message: str, tier: str = "INFO"):
    logger.info(f"WEBHOOK [{tier}]: {message}")
    # TODO: Add requests.post(WEBHOOK_URL, json={"content": message}) here

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def http_get(url: str, timeout: int = 10):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def http_post(url: str, payload: dict, timeout: int = 35):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
"""

# Read the rest of the monolith, extract classes.
# But regex parsing is hard. I'll just write a script that copies the whole thing into main.py, 
# then uses ast or just manual chunking to break it up later if needed. Actually, the user 
# expects me to implement the refactor. Let's just do a simple string matching split.

def get_block(start_str, end_str=None, include_end=False):
    start = content.find(start_str)
    if start == -1: return ""
    if end_str:
        end = content.find(end_str, start)
        if end == -1: end = len(content)
        else: end = end + len(end_str) if include_end else end
        return content[start:end]
    return content[start:]

guardrails_content = imports + """
from kida_bot.config import *
""" + get_block("class SolanaAddressValidator:", "def get_live_heuristics")

conviction_content = imports + """
from kida_bot.config import *
from kida_bot.core.network import http_get
""" + get_block("def get_live_heuristics():", "def check_swap_guardrails") + get_block("def check_memex_gates", "def calculate_dynamic_size") + get_block("def calculate_dynamic_size", "def execute_buy")

main_content = imports + """
from kida_bot.config import *
from kida_bot.core.network import http_get, http_post, send_webhook_alert
from kida_bot.core.guardrails import *
from kida_bot.strategies.conviction import *
""" + get_block("def get_status():", "def check_memex_gates") + get_block("def execute_buy", None)

with open("kida_bot/config.py", "w") as f: f.write(config_content)
with open("kida_bot/core/network.py", "w") as f: f.write(network_content)
with open("kida_bot/core/guardrails.py", "w") as f: f.write(guardrails_content)
with open("kida_bot/strategies/conviction.py", "w") as f: f.write(conviction_content)
with open("kida_bot/main.py", "w") as f: f.write(main_content)
