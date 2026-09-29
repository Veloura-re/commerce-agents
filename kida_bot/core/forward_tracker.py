"""
KIDA Citadel Forward Returns Tracker
Lightweight background task recording forward returns (+1m, +5m, +15m) for all screened candidates.
Terminates tracking after 15 minutes to guarantee minimal system overhead.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from kida_bot.core.db import (
    get_pending_forward_returns,
    mark_forward_return_completed,
    update_forward_return,
)

logger = logging.getLogger("kida_citadel.forward_tracker")

_TRACKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()


def run_tracker_cycle(
    price_fn: Callable[[str], float],
    db_path: Optional[Path] = None,
    max_batch: int = 50
) -> int:
    """
    Executes a single evaluation pass across pending candidate forward returns.
    Returns the number of candidate intervals updated.
    """
    pending = get_pending_forward_returns(limit=max_batch, db_path=db_path)
    if not pending:
        return 0

    now = time.time()
    updated_count = 0

    for item in pending:
        cid = item["candidate_id"]
        mint = item["mint"]
        init_price = item["initial_price"]
        init_ts = item["initial_ts"]
        age = now - init_ts

        # Exceeded 16 minutes: finalize to stop tracking
        if age > 960:
            mark_forward_return_completed(cid, db_path=db_path)
            continue

        # Check +1m (60s)
        if age >= 60 and item.get("price_1m") is None:
            try:
                cur_price = price_fn(mint)
                if cur_price > 0:
                    update_forward_return(cid, "1m", cur_price, init_price, db_path=db_path)
                    updated_count += 1
            except Exception as e:
                logger.debug(f"Price lookup error for {mint} at +1m: {e}")

        # Check +5m (300s)
        if age >= 300 and item.get("price_5m") is None:
            try:
                cur_price = price_fn(mint)
                if cur_price > 0:
                    update_forward_return(cid, "5m", cur_price, init_price, db_path=db_path)
                    updated_count += 1
            except Exception as e:
                logger.debug(f"Price lookup error for {mint} at +5m: {e}")

        # Check +15m (900s)
        if age >= 900 and item.get("price_15m") is None:
            try:
                cur_price = price_fn(mint)
                if cur_price > 0:
                    update_forward_return(cid, "15m", cur_price, init_price, completed=True, db_path=db_path)
                    updated_count += 1
                else:
                    mark_forward_return_completed(cid, db_path=db_path)
            except Exception as e:
                logger.debug(f"Price lookup error for {mint} at +15m: {e}")
                mark_forward_return_completed(cid, db_path=db_path)

    return updated_count


def _tracker_loop(price_fn: Callable[[str], float], poll_interval: float, db_path: Optional[Path]):
    logger.info("Forward Returns Tracker background thread started.")
    while not _STOP_EVENT.is_set():
        try:
            run_tracker_cycle(price_fn, db_path=db_path)
        except Exception as e:
            logger.error(f"Error in forward returns tracker cycle: {e}")
        _STOP_EVENT.wait(poll_interval)
    logger.info("Forward Returns Tracker background thread stopped.")


def start_forward_returns_tracker(
    price_fn: Callable[[str], float],
    poll_interval: float = 12.0,
    db_path: Optional[Path] = None
) -> threading.Thread:
    """Spawns the background daemon thread to monitor and log +1m, +5m, +15m returns."""
    global _TRACKER_THREAD, _STOP_EVENT
    if _TRACKER_THREAD is not None and _TRACKER_THREAD.is_alive():
        return _TRACKER_THREAD

    _STOP_EVENT.clear()
    _TRACKER_THREAD = threading.Thread(
        target=_tracker_loop,
        args=(price_fn, poll_interval, db_path),
        daemon=True,
        name="kida-forward-tracker"
    )
    _TRACKER_THREAD.start()
    return _TRACKER_THREAD


def stop_forward_returns_tracker():
    global _STOP_EVENT
    _STOP_EVENT.set()
