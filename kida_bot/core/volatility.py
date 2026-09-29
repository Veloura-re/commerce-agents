"""
KIDA Citadel Volatility-Aware Exits Module (Phase 4)
Replaces hardcoded fixed stops with dynamic, volatility-calibrated stops:
- Stop distance: max(config floor above round-trip cost, k * short-window range/ATR)
- Rapid momentum cut: calibrated to short-window range
- Bleeder defense: calibrated to short-window range
- Slippage-through-stop tracking and logging
- Feed latency tracking and 'blind' position flagging
"""

import collections
import logging
import time
from typing import Dict, List, Optional, Tuple

from kida_bot.config import (
    ATR_WINDOW_SECONDS,
    BLEEDER_WINDOW_SECONDS,
    BLIND_FEED_LATENCY_MS,
    K_ATR_MULTIPLIER,
    MAX_STOP_DISTANCE_PCT,
    MIN_STOP_FLOOR_PCT,
    RAPID_CUT_WINDOW_SECONDS,
)

logger = logging.getLogger("kida_citadel.volatility")


class VolatilityTracker:
    def __init__(self, window_seconds: float = ATR_WINDOW_SECONDS):
        self.window_seconds = window_seconds
        # mint -> deque of (timestamp, price)
        self._price_history: Dict[str, collections.deque] = {}
        # mint -> deque of (timestamp, latency_ms)
        self._latency_history: Dict[str, collections.deque] = collections.defaultdict(
            lambda: collections.deque(maxlen=20)
        )

    def add_price_sample(self, mint: str, price: float, ts: Optional[float] = None) -> None:
        """Records a price sample for a token and evicts samples outside the sliding window."""
        if price <= 0.0 or not mint:
            return
        now = ts if ts is not None else time.time()
        mint_clean = mint.strip().lower()

        if mint_clean not in self._price_history:
            self._price_history[mint_clean] = collections.deque()

        dq = self._price_history[mint_clean]
        dq.append((now, price))

        # Evict samples older than window_seconds
        cutoff = now - self.window_seconds
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def get_short_window_range(
        self,
        mint: str,
        cur_price: float,
        window_seconds: Optional[float] = None
    ) -> float:
        """
        Calculates normalized price range (max - min) / min over the sliding window.
        Returns a baseline default (0.015 = 1.5%) if insufficient samples exist.
        """
        mint_clean = mint.strip().lower()
        win = window_seconds if window_seconds is not None else self.window_seconds
        now = time.time()
        cutoff = now - win

        samples = []
        if mint_clean in self._price_history:
            samples = [p for (t, p) in self._price_history[mint_clean] if t >= cutoff]

        if cur_price > 0.0:
            samples.append(cur_price)

        if len(samples) < 2:
            return 0.015

        min_p = min(samples)
        max_p = max(samples)

        if min_p <= 0.0:
            return 0.015

        range_pct = (max_p - min_p) / min_p
        return max(0.005, range_pct)

    def compute_volatility_stops(
        self,
        mint: str,
        cur_price: float,
        round_trip_cost_pct: float,
        k: float = K_ATR_MULTIPLIER
    ) -> Dict[str, float]:
        """
        Computes dynamic volatility-aware stop distances:
        stop distance = max(config floor above round-trip cost, k * short-window range)
        clamped between MIN_STOP_FLOOR_PCT and MAX_STOP_DISTANCE_PCT.
        """
        token_range = self.get_short_window_range(mint, cur_price)
        cost_floor = round_trip_cost_pct + 0.005

        raw_distance = max(cost_floor, k * token_range)
        clamped_distance = max(MIN_STOP_FLOOR_PCT, min(MAX_STOP_DISTANCE_PCT, raw_distance))

        # Dynamic rapid cut: 1.0 * range, at least 1.2%
        rapid_cut_distance = max(0.012, 1.0 * token_range)

        # Dynamic bleeder cut: 1.2 * range, at least 1.0%
        bleeder_cut_distance = max(0.010, 1.2 * token_range)

        return {
            "stop_distance_pct": round(clamped_distance, 4),
            "stop_loss_pct": round(-clamped_distance, 4),
            "rapid_cut_pnl": round(-rapid_cut_distance, 4),
            "bleeder_cut_pnl": round(-bleeder_cut_distance, 4),
            "token_range_pct": round(token_range, 4),
            "round_trip_cost_pct": round(round_trip_cost_pct, 4)
        }

    def record_feed_latency(self, mint: str, latency_ms: float) -> None:
        """Records feed latency for a token."""
        mint_clean = mint.strip().lower()
        now = time.time()
        self._latency_history[mint_clean].append((now, float(latency_ms)))

    def is_feed_blind(
        self,
        mint: str,
        threshold_ms: float = BLIND_FEED_LATENCY_MS
    ) -> Tuple[bool, float]:
        """
        Determines if feed latency exceeds threshold.
        Returns (is_blind, latest_latency_ms).
        """
        mint_clean = mint.strip().lower()
        dq = self._latency_history.get(mint_clean)
        if not dq:
            return False, 0.0

        latest_latency = dq[-1][1]
        if latest_latency > threshold_ms:
            return True, latest_latency
        return False, latest_latency

    def clear_mint(self, mint: str) -> None:
        """Cleans up in-memory history when position closes."""
        mint_clean = mint.strip().lower()
        self._price_history.pop(mint_clean, None)
        self._latency_history.pop(mint_clean, None)


def calculate_slippage_through_stop(stop_trigger_price: float, executed_fill_price: float) -> float:
    """
    Measures slippage through stop: how far past the intended stop price the fill was executed.
    Returns slippage as a positive float (e.g. 0.008 = 0.8% slippage past stop).
    """
    if stop_trigger_price <= 0.0 or executed_fill_price <= 0.0:
        return 0.0

    if executed_fill_price < stop_trigger_price:
        return round((stop_trigger_price - executed_fill_price) / stop_trigger_price, 6)
    return 0.0


# Global singleton instance for rotation engine
default_volatility_tracker = VolatilityTracker()
