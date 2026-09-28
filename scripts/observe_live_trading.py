#!/usr/bin/env python3
"""
observe_live_trading.py - Continuous 1-Hour Telemetry Observer for KIDA on Railway
Polls live endpoints every 10 seconds, tracks all trade lifecycles, and records telemetry.
"""

import time
import json
import urllib.request
import urllib.error
import pathlib
import sys
from datetime import datetime

BASE_URL = "https://commerce-agents-production-a6c1.up.railway.app"
OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "aitrader" / "outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

EVENTS_LOG = OUT_DIR / "live_observation_1h.jsonl"
SUMMARY_FILE = OUT_DIR / "live_observation_summary.json"

DURATION_SECONDS = 3600
POLL_INTERVAL = 10.0

def http_get_json(url: str, timeout: int = 15):
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "KidaObserver/1.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

def log_event(event_type: str, data: dict):
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "epoch": time.time(),
        "type": event_type,
        "data": data
    }
    with open(EVENTS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

def main():
    start_time = time.time()
    end_time = start_time + DURATION_SECONDS
    print(f"Starting 1-hour live observation of KIDA at {BASE_URL}...")
    print(f"Start time: {datetime.utcnow().isoformat()}Z | Target duration: {DURATION_SECONDS}s")

    known_positions = {}
    known_closed_trades = set()
    initial_realized_pnl = None

    ticks_count = 0
    trade_events = []

    while time.time() < end_time:
        ticks_count += 1
        elapsed = time.time() - start_time
        remaining = max(0, end_time - time.time())

        # 1. Fetch current positions
        pos_data = http_get_json(f"{BASE_URL}/api/positions?chain=sol")
        logs_data = http_get_json(f"{BASE_URL}/api/logs")

        current_positions = {}
        if pos_data and isinstance(pos_data, dict):
            positions = pos_data.get("positions", [])
            for p in positions:
                addr = p.get("address")
                if addr:
                    current_positions[addr] = p

            # Check new entries
            for addr, p in current_positions.items():
                if addr not in known_positions:
                    event = {
                        "event": "POSITION_OPENED",
                        "symbol": p.get("symbol"),
                        "address": addr,
                        "size_sol": p.get("size_sol"),
                        "entry_price": p.get("entry_price"),
                        "cur_price": p.get("cur_price"),
                        "mcap": p.get("mcap"),
                        "liquidity": p.get("liquidity"),
                        "council_score": p.get("council_score"),
                        "elapsed_session_seconds": elapsed
                    }
                    print(f"[{elapsed:6.1f}s] NEW POSITION: {p.get('symbol')} ({p.get('size_sol')} SOL @ ${p.get('entry_price')})")
                    log_event("POSITION_OPENED", event)
                    trade_events.append(event)

            # Check closed positions
            for addr, p in list(known_positions.items()):
                if addr not in current_positions:
                    event = {
                        "event": "POSITION_CLOSED_OR_REMOVED",
                        "symbol": p.get("symbol"),
                        "address": addr,
                        "last_pnl": p.get("pnl"),
                        "last_price": p.get("cur_price"),
                        "elapsed_session_seconds": elapsed
                    }
                    print(f"[{elapsed:6.1f}s] POSITION CLOSED: {p.get('symbol')} (Last PnL: {p.get('pnl', 0)*100:+.2f}%)")
                    log_event("POSITION_CLOSED", event)
                    trade_events.append(event)

            known_positions = current_positions

        # 2. Parse logs for trade settlements and audit markers
        if logs_data and isinstance(logs_data, dict):
            realized_pnl = logs_data.get("total_realized_pnl_sol", 0.0)
            if initial_realized_pnl is None:
                initial_realized_pnl = realized_pnl

            logs = logs_data.get("lines", []) or logs_data.get("logs", [])
            for line in logs[-25:]:
                if any(k in line for k in ["PARTIAL_TP", "RATCHET_FLOOR", "STAGNATION_RECYCLE", "HARD_STOP", "SWAP APPLIED", "MOONBAG"]):
                    if line not in known_closed_trades:
                        known_closed_trades.add(line)
                        event = {
                            "event": "TRADE_EXECUTION_LOG",
                            "log_line": line,
                            "elapsed_session_seconds": elapsed
                        }
                        print(f"[{elapsed:6.1f}s] LOG EVENT: {line}")
                        log_event("TRADE_EXECUTION_LOG", event)
                        trade_events.append(event)

        # 3. Update summary file
        cur_pnl = (logs_data.get("total_realized_pnl_sol", 0.0) if logs_data else 0.0)
        pnl_delta = cur_pnl - (initial_realized_pnl if initial_realized_pnl is not None else cur_pnl)

        summary = {
            "observation_start_utc": datetime.utcfromtimestamp(start_time).isoformat() + "Z",
            "last_updated_utc": datetime.utcnow().isoformat() + "Z",
            "target_duration_seconds": DURATION_SECONDS,
            "elapsed_seconds": round(elapsed, 1),
            "remaining_seconds": round(remaining, 1),
            "ticks_recorded": ticks_count,
            "current_cycle": logs_data.get("cycle_count", 0) if logs_data else 0,
            "tokens_scanned": logs_data.get("tokens_scanned", 0) if logs_data else 0,
            "active_positions_count": len(known_positions),
            "active_positions": list(known_positions.values()),
            "initial_realized_pnl_sol": initial_realized_pnl,
            "current_realized_pnl_sol": cur_pnl,
            "observed_realized_pnl_delta_sol": round(pnl_delta, 6),
            "total_trade_events_captured": len(trade_events),
            "recent_events": trade_events[-10:]
        }
        with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        time.sleep(POLL_INTERVAL)

    print(f"1-hour observation complete at {datetime.utcnow().isoformat()}Z. Final summary written to {SUMMARY_FILE}")

if __name__ == "__main__":
    main()
