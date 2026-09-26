import json
import logging
from pathlib import Path
from kida_bot.config import OUTPUTS_DIR

logger = logging.getLogger(__name__)

WEIGHTS_FILE = OUTPUTS_DIR / "ai_weights.json"

DEFAULT_WEIGHTS = {
    "w_smart_money": 0.35,
    "w_liquidity": 0.25,
    "w_momentum": 0.20,
    "w_buy_ratio": 0.20,
    "learning_rate": 0.05
}

def load_weights():
    if WEIGHTS_FILE.exists():
        try:
            return json.loads(WEIGHTS_FILE.read_text())
        except Exception:
            pass
    return DEFAULT_WEIGHTS.copy()

def save_weights(weights):
    WEIGHTS_FILE.write_text(json.dumps(weights, indent=2))

def normalize_features(c):
    # Normalize features to 0-1 scale for scoring
    # Smart money: 0 to 10 typical range
    sm = min(c.get("smart_degen_count", 0) / 10.0, 1.0)
    # Liquidity: 30k to 200k typical range
    liq_val = c.get("liquidity", 30000)
    liq = max(0.0, min((liq_val - 30000) / 170000.0, 1.0))
    # Momentum: -10% to +50% 5m change
    chg = c.get("chg_5m", 0)
    mom = max(0.0, min((chg + 0.1) / 0.6, 1.0))
    # Buy ratio: 0.5 to 1.0
    br = max(0.0, min((c.get("buy_ratio", 0.5) - 0.5) / 0.5, 1.0))
    
    return sm, liq, mom, br

def evaluate_candidate_with_weights(c, weights):
    sm, liq, mom, br = normalize_features(c)
    
    score = (
        (sm * weights["w_smart_money"]) +
        (liq * weights["w_liquidity"]) +
        (mom * weights["w_momentum"]) +
        (br * weights["w_buy_ratio"])
    )
    
    # Normalize weights sum just in case they drifted
    w_sum = sum([weights["w_smart_money"], weights["w_liquidity"], weights["w_momentum"], weights["w_buy_ratio"]])
    if w_sum > 0:
        score /= w_sum
        
    return int(score * 100)

def backpropagate_trade(trade_record, candidate_snapshot):
    """
    Applies gradient descent step based on trade PnL.
    """
    weights = load_weights()
    lr = weights.get("learning_rate", 0.05)
    
    pnl = trade_record.get("realized_pnl", 0)
    # Loss function derivative approximation:
    # If PnL is positive, we want to increase weights of features that were high.
    # If PnL is negative, we decrease weights of features that were high.
    # Clip PnL to [-0.5, 0.5] as max update step multiplier (approx scaling factor)
    reward_signal = max(-0.5, min(pnl * 2, 0.5)) 
    
    sm, liq, mom, br = normalize_features(candidate_snapshot)
    
    # Update rule: w_new = w_old + lr * reward * feature_value
    weights["w_smart_money"] = max(0.05, weights["w_smart_money"] + lr * reward_signal * sm)
    weights["w_liquidity"] = max(0.05, weights["w_liquidity"] + lr * reward_signal * liq)
    weights["w_momentum"] = max(0.05, weights["w_momentum"] + lr * reward_signal * mom)
    weights["w_buy_ratio"] = max(0.05, weights["w_buy_ratio"] + lr * reward_signal * br)
    
    # Re-normalize to sum to 1.0
    w_sum = sum([weights["w_smart_money"], weights["w_liquidity"], weights["w_momentum"], weights["w_buy_ratio"]])
    weights["w_smart_money"] /= w_sum
    weights["w_liquidity"] /= w_sum
    weights["w_momentum"] /= w_sum
    weights["w_buy_ratio"] /= w_sum
    
    save_weights(weights)
    logger.info(f"[BACKPROP] Trade PnL {pnl:+.4f} | Weights Adjusted -> SM: {weights['w_smart_money']:.2f}, LIQ: {weights['w_liquidity']:.2f}, MOM: {weights['w_momentum']:.2f}, BR: {weights['w_buy_ratio']:.2f}")
