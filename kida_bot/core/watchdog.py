import time
import json
import pathlib
import logging
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger("WatchdogCitadel")
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

def write_heartbeat(
    cycle_count: int,
    active_positions_count: int,
    mode: str = "SHADOW",
    details: Optional[Dict[str, Any]] = None
) -> None:
    """
    Writes a heartbeat record atomically to file for process observability.
    """
    from kida_bot.config import HEARTBEAT_FILE
    now = time.time()
    payload = {
        "timestamp": now,
        "datetime": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
        "cycle_count": cycle_count,
        "active_positions_count": active_positions_count,
        "mode": mode,
        "details": details or {}
    }
    
    try:
        tmp_file = HEARTBEAT_FILE.with_name(f"heartbeat_{int(now * 1000)}.tmp")
        tmp_file.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        tmp_file.replace(HEARTBEAT_FILE)
    except Exception as e:
        logger.error(f"Failed to write heartbeat: {e}")


def read_heartbeat() -> Dict[str, Any]:
    """
    Reads the latest heartbeat and calculates latency/staleness.
    """
    from kida_bot.config import HEARTBEAT_FILE, WATCHDOG_STALL_TIMEOUT_SECONDS
    if not HEARTBEAT_FILE.exists():
        return {
            "exists": False,
            "status": "MISSING",
            "age_seconds": None,
            "is_stalled": True,
            "data": None
        }

    try:
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        hb_time = float(data.get("timestamp", 0.0))
        age = time.time() - hb_time
        stalled = age > WATCHDOG_STALL_TIMEOUT_SECONDS
        
        return {
            "exists": True,
            "status": "STALLED" if stalled else "HEALTHY",
            "age_seconds": round(age, 2),
            "is_stalled": stalled,
            "stall_threshold_seconds": WATCHDOG_STALL_TIMEOUT_SECONDS,
            "data": data
        }
    except Exception as e:
        return {
            "exists": True,
            "status": "CORRUPTED",
            "error": str(e),
            "age_seconds": None,
            "is_stalled": True,
            "data": None
        }


def check_watchdog() -> Tuple[bool, str]:
    """
    Returns (is_healthy, reason). If stalled, returns (False, error_reason).
    """
    hb = read_heartbeat()
    if not hb.get("exists"):
        return False, "Heartbeat file missing: rotation engine has not emitted heartbeat."
    if hb.get("is_stalled"):
        age = hb.get("age_seconds")
        thresh = hb.get("stall_threshold_seconds", 15.0)
        return False, f"Rotation loop stalled: last heartbeat was {age:.1f}s ago (threshold {thresh:.1f}s)."
    return True, f"Rotation loop healthy (heartbeat age: {hb.get('age_seconds')}s)."
