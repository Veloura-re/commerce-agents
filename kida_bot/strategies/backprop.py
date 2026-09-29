"""
KIDA Citadel Learning Loop (Phase 6)
Backpropagation with safety guards:
- No-op until N closed trades (config, default 50)
- PnL normalization: clip to configurable range, divide by risk
- Weight decay toward baseline each update
- Max step size cap per update
- Per-trade-id weight change logging
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from kida_bot.config import (
    BACKPROP_MAX_STEP_SIZE,
    BACKPROP_PNL_CLIP_RANGE,
    BACKPROP_WEIGHT_DECAY,
    MIN_CLOSED_TRADES_FOR_BACKPROP,
    OUTPUTS_DIR,
)

logger = logging.getLogger("kida_citadel.backprop")

WEIGHTS_FILE = OUTPUTS_DIR / "ai_weights.json"
WEIGHT_LOG_FILE = OUTPUTS_DIR / "weight_changes.jsonl"

DEFAULT_WEIGHTS = {
    "w_smart_money": 0.35,
    "w_liquidity": 0.25,
    "w_momentum": 0.20,
    "w_buy_ratio": 0.20,
    "learning_rate": 0.05,
}

FEATURE_KEYS = ["w_smart_money", "w_liquidity", "w_momentum", "w_buy_ratio"]


def load_weights() -> Dict[str, float]:
    if WEIGHTS_FILE.exists():
        try:
            return json.loads(WEIGHTS_FILE.read_text())
        except Exception:
            pass
    return DEFAULT_WEIGHTS.copy()


def save_weights(weights: Dict[str, float]) -> None:
    WEIGHTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WEIGHTS_FILE.write_text(json.dumps(weights, indent=2))


def normalize_features(c: Dict[str, Any]) -> tuple:
    """Normalizes candidate features to 0-1 scale for scoring."""
    sm = min(c.get("smart_degen_count", 0) / 10.0, 1.0)
    liq_val = c.get("liquidity", 30000)
    liq = max(0.0, min((liq_val - 30000) / 170000.0, 1.0))
    chg = c.get("chg_5m", 0)
    mom = max(0.0, min((chg + 0.1) / 0.6, 1.0))
    br = max(0.0, min((c.get("buy_ratio", 0.5) - 0.5) / 0.5, 1.0))
    return sm, liq, mom, br


def evaluate_candidate_with_weights(c: Dict[str, Any], weights: Dict[str, float]) -> int:
    sm, liq, mom, br = normalize_features(c)
    score = (
        (sm * weights["w_smart_money"])
        + (liq * weights["w_liquidity"])
        + (mom * weights["w_momentum"])
        + (br * weights["w_buy_ratio"])
    )
    w_sum = sum(weights[k] for k in FEATURE_KEYS)
    if w_sum > 0:
        score /= w_sum
    return int(score * 100)


def _log_weight_change(
    trade_id: str,
    old_weights: Dict[str, float],
    new_weights: Dict[str, float],
    pnl: float,
    reward_signal: float,
) -> None:
    """Appends a structured weight change record to the JSONL log."""
    entry = {
        "ts": time.time(),
        "trade_id": trade_id,
        "pnl": round(pnl, 6),
        "reward_signal": round(reward_signal, 6),
        "old": {k: round(old_weights[k], 6) for k in FEATURE_KEYS},
        "new": {k: round(new_weights[k], 6) for k in FEATURE_KEYS},
        "deltas": {k: round(new_weights[k] - old_weights[k], 6) for k in FEATURE_KEYS},
    }
    WEIGHT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WEIGHT_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def backpropagate_trade(
    trade_record: Dict[str, Any],
    candidate_snapshot: Dict[str, Any],
    closed_trades_count: int = 0,
    trade_size_sol: float = 0.35,
) -> bool:
    """
    Applies a guarded gradient descent step based on trade PnL.

    Safety guards (Phase 6):
    1. No-op until min_closed_trades reached (prevents premature tuning)
    2. PnL normalization: clip to range, divide by risk (trade size)
    3. Weight decay toward baseline each update
    4. Max step size cap per individual weight change
    5. Per-trade-id logging of every weight change

    Returns True if update was applied, False if skipped.
    """
    # Guard 1: Minimum closed trades threshold
    if closed_trades_count < MIN_CLOSED_TRADES_FOR_BACKPROP:
        logger.debug(
            f"[BACKPROP] Skipped: only {closed_trades_count} closed trades "
            f"(threshold: {MIN_CLOSED_TRADES_FOR_BACKPROP})"
        )
        return False

    weights = load_weights()
    old_weights = {k: weights[k] for k in FEATURE_KEYS}
    lr = weights.get("learning_rate", 0.05)

    # Guard 2: PnL normalization -- clip and divide by risk
    raw_pnl = float(trade_record.get("realized_pnl", 0.0))
    risk = max(0.01, float(trade_size_sol))
    normalized_pnl = raw_pnl / risk
    reward_signal = max(-BACKPROP_PNL_CLIP_RANGE, min(BACKPROP_PNL_CLIP_RANGE, normalized_pnl))

    sm, liq, mom, br = normalize_features(candidate_snapshot)
    features = [sm, liq, mom, br]

    for i, key in enumerate(FEATURE_KEYS):
        # Raw gradient step
        raw_step = lr * reward_signal * features[i]

        # Guard 4: Max step size cap
        clamped_step = max(-BACKPROP_MAX_STEP_SIZE, min(BACKPROP_MAX_STEP_SIZE, raw_step))

        # Guard 3: Weight decay toward baseline
        baseline = DEFAULT_WEIGHTS[key]
        decay_pull = BACKPROP_WEIGHT_DECAY * (baseline - weights[key])

        weights[key] = max(0.05, weights[key] + clamped_step + decay_pull)

    # Re-normalize weights to sum to 1.0
    w_sum = sum(weights[k] for k in FEATURE_KEYS)
    if w_sum > 0:
        for k in FEATURE_KEYS:
            weights[k] = round(weights[k] / w_sum, 6)

    save_weights(weights)

    # Guard 5: Per-trade-id logging
    trade_id = trade_record.get("trade_id", f"unknown_{int(time.time() * 1000)}")
    _log_weight_change(trade_id, old_weights, weights, raw_pnl, reward_signal)

    logger.info(
        f"[BACKPROP] Trade {trade_id} | PnL {raw_pnl:+.4f} SOL | "
        f"Signal {reward_signal:+.4f} | "
        f"SM: {old_weights['w_smart_money']:.4f}->{weights['w_smart_money']:.4f} | "
        f"LIQ: {old_weights['w_liquidity']:.4f}->{weights['w_liquidity']:.4f} | "
        f"MOM: {old_weights['w_momentum']:.4f}->{weights['w_momentum']:.4f} | "
        f"BR: {old_weights['w_buy_ratio']:.4f}->{weights['w_buy_ratio']:.4f}"
    )
    return True
