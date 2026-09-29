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
import os
import yaml
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum, auto
from collections import deque
from tenacity import retry, stop_after_attempt, wait_exponential

HERE = pathlib.Path(__file__).resolve().parent.parent
CONFIG_YAML_PATH = HERE / "config.yaml"

LOG_FILE = HERE / "aitrader" / "outputs" / "rotation_session_1h.log"
AUDIT_FILE = HERE / "aitrader" / "outputs" / "session_audit.json"
HEARTBEAT_FILE = HERE / "aitrader" / "outputs" / "heartbeat.json"
OUTPUTS_DIR = HERE / "aitrader" / "outputs"
HALT_FILE_PATH = HERE / "HALT"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("ContinuousRotationEngine")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")

if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    fh = logging.FileHandler(str(LOG_FILE), mode="a", encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

def _load_and_validate_config(config_path: pathlib.Path) -> Dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"CRITICAL: Master configuration file missing at {config_path}")
    
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
        
    if not isinstance(cfg, dict):
        raise ValueError(f"CRITICAL: Invalid config structure in {config_path}. Expected dictionary.")

    # Validation: Mode
    mode_sec = cfg.get("mode", {})
    paper_val = mode_sec.get("paper_mode", True)
    if not isinstance(paper_val, bool):
        raise ValueError(f"CRITICAL: mode.paper_mode must be boolean, got {type(paper_val)}")

    # Validation: Capital
    cap = cfg.get("capital", {})
    if cap.get("total_bankroll_sol", 0) <= 0:
        raise ValueError("CRITICAL: capital.total_bankroll_sol must be positive.")
    if cap.get("default_trade_size_sol", 0) <= 0:
        raise ValueError("CRITICAL: capital.default_trade_size_sol must be positive.")
    if cap.get("min_trade_size_sol", 0) > cap.get("max_trade_size_sol", 0):
        raise ValueError("CRITICAL: capital.min_trade_size_sol cannot exceed max_trade_size_sol.")
    if cap.get("max_positions", 0) < 1:
        raise ValueError("CRITICAL: capital.max_positions must be at least 1.")

    # Validation: Killswitch
    ks = cfg.get("killswitch", {})
    if ks.get("daily_loss_cap_sol", 0) <= 0:
        raise ValueError("CRITICAL: killswitch.daily_loss_cap_sol must be positive.")
    if not (0 < ks.get("max_drawdown_pct", 0) < 1.0):
        raise ValueError("CRITICAL: killswitch.max_drawdown_pct must be between 0.0 and 1.0.")
    if ks.get("max_consecutive_losses", 0) < 1:
        raise ValueError("CRITICAL: killswitch.max_consecutive_losses must be >= 1.")

    # Validation: Stops
    stops = cfg.get("stops", {})
    if stops.get("stop_loss_pct", 0) >= 0:
        raise ValueError("CRITICAL: stops.stop_loss_pct must be negative (e.g. -0.035).")
    if stops.get("take_profit_pct", 0) <= 0:
        raise ValueError("CRITICAL: stops.take_profit_pct must be positive.")
    if stops.get("stagnation_timeout_seconds", 0) <= 0:
        raise ValueError("CRITICAL: stops.stagnation_timeout_seconds must be positive.")

    # Validation: Gates
    gates = cfg.get("gates", {})
    if gates.get("min_pool_liquidity_usd", 0) <= 0:
        raise ValueError("CRITICAL: gates.min_pool_liquidity_usd must be positive.")
    if not (0.0 <= gates.get("min_buy_ratio", 0) <= 1.0):
        raise ValueError("CRITICAL: gates.min_buy_ratio must be between 0.0 and 1.0.")
    if not (0.0 <= gates.get("min_lp_burn_or_lock_pct", 0.95) <= 1.0):
        raise ValueError("CRITICAL: gates.min_lp_burn_or_lock_pct must be between 0.0 and 1.0.")
    if gates.get("max_snapshot_age_seconds", 60.0) <= 0:
        raise ValueError("CRITICAL: gates.max_snapshot_age_seconds must be positive.")

    # Validation: Cost Model
    cost = cfg.get("cost_model", {})
    if not (0.0 < cost.get("max_cost_to_first_target_ratio", 0.40) <= 1.0):
        raise ValueError("CRITICAL: cost_model.max_cost_to_first_target_ratio must be between 0.0 and 1.0.")
    if cost.get("green_vault_margin", 0) <= 0:
        raise ValueError("CRITICAL: cost_model.green_vault_margin must be positive.")
    if cost.get("tier1_floor_margin", 0) <= 0:
        raise ValueError("CRITICAL: cost_model.tier1_floor_margin must be positive.")

    # Validation: Volatility Stops
    v_stops = cfg.get("volatility_stops", {})
    if v_stops:
        if v_stops.get("k_atr_multiplier", 1.5) <= 0:
            raise ValueError("CRITICAL: volatility_stops.k_atr_multiplier must be > 0.")
        if v_stops.get("min_stop_floor_pct", 0.015) <= 0:
            raise ValueError("CRITICAL: volatility_stops.min_stop_floor_pct must be > 0.")
        if v_stops.get("blind_feed_latency_ms", 3000.0) <= 0:
            raise ValueError("CRITICAL: volatility_stops.blind_feed_latency_ms must be > 0.")

    # Validation: Regimes (Inconsistency & Binding Check)
    regimes = cfg.get("regimes", {})
    max_active_exposure = cap.get("max_active_exposure_sol", 3.60)
    for r_name, r_cfg in regimes.items():
        size = r_cfg.get("trade_size_sol", 0.0)
        slots = r_cfg.get("max_positions", 0)
        exp_cap = r_cfg.get("exposure_cap_sol", 0.0)
        if size <= 0 or slots <= 0 or exp_cap <= 0:
            raise ValueError(f"CRITICAL: regimes.{r_name} parameters must be positive.")
        if exp_cap > max_active_exposure + 0.001:
            raise ValueError(f"CRITICAL: regimes.{r_name}.exposure_cap_sol ({exp_cap}) exceeds capital max_active_exposure_sol ({max_active_exposure}).")

    return cfg

# Load validated configuration
raw_config = _load_and_validate_config(CONFIG_YAML_PATH)

# Environment overrides
env_paper = os.environ.get("PAPER_MODE")
if env_paper is not None:
    PAPER_MODE = env_paper.strip().lower() in ("true", "1", "yes", "on")
else:
    PAPER_MODE = bool(raw_config.get("mode", {}).get("paper_mode", True))

port = os.environ.get("PORT", "8000")
API_BASE = f"http://127.0.0.1:{port}"
CHAIN = "sol"

# Capital Parameters
_cap = raw_config.get("capital", {})
TOTAL_BANKROLL_USD = float(_cap.get("total_bankroll_usd", 1400.0))
TOTAL_BANKROLL_SOL = float(_cap.get("total_bankroll_sol", 10.0))
CAPITAL_UTILIZATION_PCT = float(_cap.get("capital_utilization_pct", 0.50))
MAX_ACTIVE_EXPOSURE_SOL = float(_cap.get("max_active_exposure_sol", 3.60))
DEFAULT_TRADE_SIZE_SOL = float(_cap.get("default_trade_size_sol", 0.350))
MIN_TRADE_SIZE_SOL = float(_cap.get("min_trade_size_sol", 0.250))
MAX_TRADE_SIZE_SOL = float(_cap.get("max_trade_size_sol", 0.500))
MAX_POSITIONS = int(_cap.get("max_positions", 5))
SESSION_DURATION_SECONDS = 0

# Killswitch Parameters
_ks = raw_config.get("killswitch", {})
DAILY_LOSS_CAP_SOL = float(_ks.get("daily_loss_cap_sol", 1.80))
MAX_DRAWDOWN_PCT = float(_ks.get("max_drawdown_pct", 0.15))
MAX_CONSECUTIVE_LOSSES = int(_ks.get("max_consecutive_losses", 6))
HALT_FILE_NAME = str(_ks.get("halt_file", "HALT"))
HALT_FILE_PATH = HERE / HALT_FILE_NAME

# Watchdog Parameters
_wd = raw_config.get("watchdog", {})
FAST_MONITOR_INTERVAL_SECONDS = float(_wd.get("fast_monitor_interval_seconds", 2.0))
SCREEN_INTERVAL_SECONDS = float(_wd.get("screen_interval_seconds", 6.0))
WATCHDOG_STALL_TIMEOUT_SECONDS = float(_wd.get("stall_timeout_seconds", 15.0))
HEARTBEAT_FILE = HERE / str(_wd.get("heartbeat_file", "aitrader/outputs/heartbeat.json"))

# Stops & Risk Parameters
_stops = raw_config.get("stops", {})
TAKE_PROFIT_PCT = float(_stops.get("take_profit_pct", 0.350))
STOP_LOSS_PCT = float(_stops.get("stop_loss_pct", -0.035))
REENTRY_STOP_LOSS_PCT = float(_stops.get("reentry_stop_loss_pct", -0.020))
RAPID_MOMENTUM_CUT_SECONDS = float(_stops.get("rapid_momentum_cut_seconds", 25.0))
RAPID_MOMENTUM_CUT_PNL = float(_stops.get("rapid_momentum_cut_pnl", -0.020))
EARLY_BLEEDER_CUT_SECONDS = float(_stops.get("early_bleeder_cut_seconds", 90.0))
EARLY_BLEEDER_CUT_PNL = float(_stops.get("early_bleeder_cut_pnl", -0.018))
STAGNATION_TIMEOUT_SECONDS = float(_stops.get("stagnation_timeout_seconds", 300.0))
STAGNATION_MIN_PNL_TARGET = float(_stops.get("stagnation_min_pnl_target", 0.020))
STAGNATION_GREEN_HARVEST_PNL = float(_stops.get("stagnation_green_harvest_pnl", 0.006))
STAGNATION_LOCKED_FLOOR_PNL = float(_stops.get("stagnation_locked_floor_pnl", 0.004))
EMERGENCY_LIQUIDITY_DROP_THRESHOLD = float(_stops.get("emergency_liquidity_drop_threshold", 0.15))
MOONBAG_TRAILING_STOP_PULLBACK = float(_stops.get("moonbag_trailing_stop_pullback", 0.12))
NEGATIVE_TOKEN_LOCKOUT_SECONDS = int(_stops.get("negative_token_lockout_seconds", 21600))
MAX_REENTRIES_PER_TOKEN = int(_stops.get("max_reentries_per_token", 1))

# Volatility Stops (Phase 4)
_vol_stops = raw_config.get("volatility_stops", {})
K_ATR_MULTIPLIER = float(_vol_stops.get("k_atr_multiplier", 1.5))
ATR_WINDOW_SECONDS = float(_vol_stops.get("atr_window_seconds", 60.0))
MIN_STOP_FLOOR_PCT = float(_vol_stops.get("min_stop_floor_pct", 0.015))
MAX_STOP_DISTANCE_PCT = float(_vol_stops.get("max_stop_distance_pct", 0.050))
RAPID_CUT_WINDOW_SECONDS = float(_vol_stops.get("rapid_cut_window_seconds", 25.0))
BLEEDER_WINDOW_SECONDS = float(_vol_stops.get("bleeder_window_seconds", 90.0))
BLIND_FEED_LATENCY_MS = float(_vol_stops.get("blind_feed_latency_ms", 3000.0))

# Ratchet Tiers
_tiers = raw_config.get("ratchet_tiers", {})
TIER1_TRIGGER_PCT = float(_tiers.get("tier1_trigger_pct", 0.050))
TIER1_LOCK_PCT = float(_tiers.get("tier1_lock_pct", 0.020))
TIER2_TRIGGER_PCT = float(_tiers.get("tier2_trigger_pct", 0.100))
TIER2_LOCK_PCT = float(_tiers.get("tier2_lock_pct", 0.050))
TIER3_TRIGGER_PCT = float(_tiers.get("tier3_trigger_pct", 0.200))
TIER3_LOCK_PCT = float(_tiers.get("tier3_lock_pct", 0.120))

# Gates
_gates = raw_config.get("gates", {})
MIN_POOL_LIQUIDITY_USD = float(_gates.get("min_pool_liquidity_usd", 25000.0))
MIN_MCAP = float(_gates.get("min_mcap", 30000.0))
MAX_MCAP = float(_gates.get("max_mcap", 1_000_000_000_000.0))
MIN_1H_VOLUME = float(_gates.get("min_1h_volume", 10000.0))
MIN_SMART_MONEY = int(_gates.get("min_smart_money", 4))
MIN_BUY_RATIO = float(_gates.get("min_buy_ratio", 0.48))
MAX_BUNDLER_RATE = float(_gates.get("max_bundler_rate", 0.25))
MAX_DEV_HOLD_RATE = float(_gates.get("max_dev_hold_rate", 0.10))
MAX_TOP10_CONCENTRATION = float(_gates.get("max_top10_concentration", 0.50))
MAX_BUY_TAX = float(_gates.get("max_buy_tax", 0.08))
MAX_SELL_TAX = float(_gates.get("max_sell_tax", 0.08))
MAX_RUG_RATIO = float(_gates.get("max_rug_ratio", 0.60))
REQUIRE_RENOUNCED_MINT = bool(_gates.get("require_renounced_mint", True))
REQUIRE_RENOUNCED_FREEZE = bool(_gates.get("require_renounced_freeze", True))
MIN_LP_BURN_OR_LOCK_PCT = float(_gates.get("min_lp_burn_or_lock_pct", 0.95))
MAX_SELL_SIMULATION_IMPACT_PCT = float(_gates.get("max_sell_simulation_impact_pct", 0.05))
MAX_SNAPSHOT_AGE_SECONDS = float(_gates.get("max_snapshot_age_seconds", 60.0))
MAX_CHG_5M_OVERBOUGHT = float(_gates.get("max_chg_5m_overbought", 0.20))
MIN_CHG_5M_FALLING_KNIFE = float(_gates.get("min_chg_5m_falling_knife", -0.025))
MIN_COUNCIL_SCORE = int(_gates.get("min_council_score", 55))
MIN_COUNCIL_SENTINELS_PASS = int(_gates.get("min_council_sentinels_pass", 3))

# Regime Configuration (Phase 5)
_regimes = raw_config.get("regimes", {})
REGIME_HIGH_CONVICTION = {
    "regime": "HIGH_CONVICTION",
    "trade_size_sol": float(_regimes.get("high_conviction", {}).get("trade_size_sol", 0.70)),
    "max_positions": int(_regimes.get("high_conviction", {}).get("max_positions", 4)),
    "exposure_cap_sol": float(_regimes.get("high_conviction", {}).get("exposure_cap_sol", 2.80)),
    "min_sol_trend_pct_1h": float(_regimes.get("high_conviction", {}).get("min_sol_trend_pct_1h", 0.010)),
}
REGIME_STANDARD = {
    "regime": "STANDARD_CONSERVATIVE",
    "trade_size_sol": float(_regimes.get("standard_conservative", {}).get("trade_size_sol", 0.50)),
    "max_positions": int(_regimes.get("standard_conservative", {}).get("max_positions", 5)),
    "exposure_cap_sol": float(_regimes.get("standard_conservative", {}).get("exposure_cap_sol", 2.50)),
    "min_sol_trend_pct_1h": float(_regimes.get("standard_conservative", {}).get("min_sol_trend_pct_1h", -0.010)),
}
REGIME_MICRO_SCALP = {
    "regime": "MICRO_SCALP",
    "trade_size_sol": float(_regimes.get("micro_scalp", {}).get("trade_size_sol", 0.30)),
    "max_positions": int(_regimes.get("micro_scalp", {}).get("max_positions", 6)),
    "exposure_cap_sol": float(_regimes.get("micro_scalp", {}).get("exposure_cap_sol", 1.80)),
    "min_sol_trend_pct_1h": float(_regimes.get("micro_scalp", {}).get("min_sol_trend_pct_1h", -0.050)),
}

# Learning Loop (Phase 6)
_learning = raw_config.get("learning", {})
MIN_CLOSED_TRADES_FOR_BACKPROP = int(_learning.get("min_closed_trades", 50))
BACKPROP_MAX_STEP_SIZE = float(_learning.get("max_step_size", 0.10))
BACKPROP_WEIGHT_DECAY = float(_learning.get("weight_decay", 0.01))
BACKPROP_PNL_CLIP_RANGE = float(_learning.get("pnl_clip_range", 0.50))

# Cost Model & Realized Friction
_cost = raw_config.get("cost_model", {})
MAX_COST_TO_FIRST_TARGET_RATIO = float(_cost.get("max_cost_to_first_target_ratio", 0.40))
EXPECTED_FIRST_TARGET_PCT = float(_cost.get("expected_first_target_pct", 0.050))
GREEN_VAULT_MARGIN = float(_cost.get("green_vault_margin", 0.004))
TIER1_FLOOR_MARGIN = float(_cost.get("tier1_floor_margin", 0.010))
PRIORITY_FEE_SOL = float(_cost.get("priority_fee_sol", 0.002))
SWAP_FEE_PCT = float(_cost.get("swap_fee_pct", 0.0025))
MAX_PRICE_IMPACT_PCT = float(_cost.get("max_price_impact_pct", 0.025))

state = {
    "session_start_time": time.time(),
    "cycle_count": 0,
    "tokens_scanned": 0,
    "total_trades": 0,
    "closed_trades": [],
    "total_realized_pnl_sol": 0.0,
    "peak_session_equity_sol": TOTAL_BANKROLL_SOL,
    "last_heartbeat_time": time.time(),
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
    "held_addresses": set(),
    "cooldown_until": {},
    "token_history": {}
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
        with open(AUDIT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save audit state: {e}")
