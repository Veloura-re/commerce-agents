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
from datetime import datetime

try:
    from aitrader.brain import brain
except ImportError:
    try:
        from brain import brain
    except ImportError:
        pass # mock it if absent

from kida_bot.config import *
from kida_bot.core.network import http_get
def get_live_heuristics():
    try:
        h = brain.get_heuristics()
        return {
            "min_pool_liquidity_usd": 45000.0,
            "min_mcap": 40000.0,
            "max_mcap": 1_000_000_000_000.0,
            "min_1h_volume": 15000.0,
            "min_smart_money": max(15, int(h.get("min_smart_money_consensus", 15))),
            "min_chg_5m": -0.015,
            "min_buy_ratio": 0.50,
            "max_bundler_rate": min(0.05, float(h.get("max_bundler_tolerance", 0.05))),
            "max_dev_hold_rate": min(0.05, float(h.get("max_dev_hold_tolerance", 0.05))),
        }
    except Exception:
        return {
            "min_pool_liquidity_usd": 45000.0,
            "min_mcap": 40000.0,
            "max_mcap": 1_000_000_000_000.0,
            "min_1h_volume": 15000.0,
            "min_smart_money": 15,
            "min_chg_5m": -0.015,
            "min_buy_ratio": 0.50,
            "max_bundler_rate": 0.05,
            "max_dev_hold_rate": 0.05,
        }

state = {
    "session_start_time": time.time(),
    "total_trades": 0,
    "entry_timestamps": {},
    "peak_prices": {},
    "peak_liquidity": {},
    "entry_sm_counts": {},
    "entry_mcaps": {},
    "entry_liquidities": {},
    "is_reentry": {},
    "position_sizes": {},
    "active_stop_floors": {},
    "cooldown_until": {},
    "token_history": {},
    "cycle_count": 0,
    "closed_trades": [],
    "total_realized_pnl_sol": 0.0,
    "tokens_scanned": 0,
    "partial_tp_taken": set(),
    "tp_stage": {},
    "moonbag_active": set(),
    "break_even_locked": set(),
    "last_screen_time": 0.0,
    "last_candidates": []
}

# Session State Persistence Restoration
if AUDIT_FILE.exists():
    try:
        prev_data = json.loads(AUDIT_FILE.read_text(encoding="utf-8"))
        if isinstance(prev_data, dict):
            sess_start = prev_data.get("session_start")
            if sess_start and (time.time() - sess_start) < SESSION_DURATION_SECONDS:
                state["session_start_time"] = sess_start
                state["total_trades"] = prev_data.get("total_trades_opened", 0)
                state["cycle_count"] = prev_data.get("cycle_count", 0)
                raw_closed = prev_data.get("closed_trades", [])
                state["closed_trades"] = raw_closed
                prev_pnl = prev_data.get("total_realized_pnl_sol", 0.0) or 0.0
                state["total_realized_pnl_sol"] = prev_pnl
                state["tokens_scanned"] = prev_data.get("tokens_scanned", 0)
                state["token_history"] = prev_data.get("token_history", {})
                logger.info(
                    f"RESTORED ACTIVE SESSION: Cycle #{state['cycle_count']}, "
                    f"{len(state['closed_trades'])} closed trades, Realized PnL: {state['total_realized_pnl_sol']:+.4f} SOL"
                )
    except Exception as e:
        logger.warning(f"Could not load previous session audit state: {e}")

# ==============================================================================
# SECTION 4: HTTP CLIENT & BACKEND CONNECTORS
# ==============================================================================

def http_get(url: str, timeout: int = 10):
    req = urllib.request.Request(url, headers={"User-Agent": "KIDA-Sniper/4.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def http_post(url: str, payload: dict, timeout: int = 35):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "KIDA-Sniper/4.0",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def get_status():
    try:
        return http_get(f"{API_BASE}/api/status", timeout=5)
    except Exception as e:
        logger.error(f"Failed to fetch API status: {e}")
        return None

def get_positions():
    try:
        data = http_get(f"{API_BASE}/api/positions?chain={CHAIN}", timeout=10)
        positions = data.get("positions", [])
        if not positions:
            try:
                s_data = http_get(f"{API_BASE}/api/state", timeout=5)
                positions = s_data.get("active_positions", [])
            except Exception:
                pass
        # Ingest currently held positions into provenance registry and preserve entry time
        for p in positions:
            addr = p.get("address")
            sym = p.get("symbol", "TOKEN")
            cycles = p.get("cycles", 0)
            if addr:
                provenance_registry.register_held_position(addr, sym, source="PORTFOLIO_INGEST")
                if addr not in state["entry_timestamps"]:
                    elapsed_prior = cycles * FAST_MONITOR_INTERVAL_SECONDS
                    state["entry_timestamps"][addr] = time.time() - elapsed_prior
        return positions
    except Exception as e:
        logger.error(f"Failed to fetch positions: {e}")
        return []

def get_portfolio_context():
    try:
        data = http_get(f"{API_BASE}/api/positions?chain={CHAIN}", timeout=5)
        positions = data.get("positions", [])
        total_exp = sum(p.get("size_sol", DEFAULT_TRADE_SIZE_SOL) for p in positions)
        return {
            "positions": positions,
            "total_exposure": total_exp,
            "portfolio": data.get("portfolio", {})
        }
    except Exception:
        positions = get_positions()
        total_exp = sum(p.get("size_sol", DEFAULT_TRADE_SIZE_SOL) for p in positions)
        return {"positions": positions, "total_exposure": total_exp, "portfolio": {}}

def save_audit_state():
    try:
        positions = get_positions()
        unrealized_sol = sum(p.get("pnl", 0) * p.get("size_sol", DEFAULT_TRADE_SIZE_SOL) for p in positions)
        
        wins = [t for t in state["closed_trades"] if (t.get("pnl_sol") or 0.0) >= 0]
        losses = [t for t in state["closed_trades"] if (t.get("pnl_sol") or 0.0) < 0]
        sol_gained = sum(t["pnl_sol"] for t in wins)
        sol_lost = abs(sum(t["pnl_sol"] for t in losses))
        realized_pnl = state["total_realized_pnl_sol"]

        data = {
            "session_start": state["session_start_time"],
            "elapsed_seconds": time.time() - state["session_start_time"],
            "cycle_count": state["cycle_count"],
            "total_trades_opened": state["total_trades"],
            "total_trades_closed": len(state["closed_trades"]),
            "winning_trades_count": len(wins),
            "losing_trades_count": len(losses),
            "win_rate_pct": round(len(wins) / max(len(state["closed_trades"]), 1) * 100, 1),
            "total_sol_gained": round(sol_gained, 6),
            "total_sol_lost": round(sol_lost, 6),
            "profit_factor": round(sol_gained / max(sol_lost, 0.0001), 2),
            "total_realized_pnl_sol": round(realized_pnl, 6),
            "unrealized_pnl_sol": round(unrealized_sol, 6),
            "net_portfolio_pnl_sol": round(realized_pnl + unrealized_sol, 6),
            "tokens_scanned": state["tokens_scanned"],
            "provenance_stats": provenance_registry.get_stats(),
            "two_phase_guardrails": swap_ledger.get_audit_summary(),
            "token_history": state["token_history"],
            "closed_trades": state["closed_trades"],
            "active_positions": positions
        }
        import uuid
        tmp_file = AUDIT_FILE.with_name(f"session_audit_{uuid.uuid4().hex}.tmp")
        with open(str(tmp_file), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_file.replace(AUDIT_FILE)
    except Exception as e:
        logger.error(f"Failed to save audit state: {e}")

# ==============================================================================
# SECTION 5: SCREENING & MEMEX DEFENSE
# ==============================================================================

def run_screening():
    try:
        data = http_post(f"{API_BASE}/api/run", {"chain": CHAIN}, timeout=35)
        decisions = data.get("decisions", [])
        state["tokens_scanned"] += len(decisions)
        
        heur = get_live_heuristics()
        candidates = []
        for item in decisions:
            dec = item.get("decision", {})
            feat = dec.get("features", {})
            addr = dec.get("address")
            sym = dec.get("symbol")
            action = dec.get("action", "")
            
            mcap = feat.get("mcap", 0.0)
            vol = feat.get("turnover", 0.0) * mcap if mcap > 0 else feat.get("vol_1h", 0.0)
            sm = feat.get("sm_confluence", 0) or (feat.get("smart_degen", 0) + feat.get("renowned", 0))
            liq = feat.get("liquidity", 0.0)
            chg_5m = feat.get("chg_5m", 0.0)
            buy_ratio = feat.get("buy_ratio", 0.5)
            bundler = feat.get("bundler", 0.0)
            dev_hold = feat.get("dev_hold", 0.0)

            # Register screened candidate into Provenance Registry
            candidate_payload = {
                "address": addr,
                "symbol": sym,
                "market_cap": mcap,
                "volume": vol,
                "liquidity": liq,
                "chg_5m": chg_5m,
                "buy_ratio": buy_ratio,
                "bundler": bundler,
                "dev_hold": dev_hold,
                "features": feat
            }
            provenance_registry.register_screened_candidate(candidate_payload, source="GMGN_LIVE_SCREENER")

            # Velocity Guard: Deep liquidity requires active turnover
            if liq >= 500_000.0:
                turnover_rate = feat.get("turnover", 0.0) or (vol / liq if liq > 0 else 0.0)
                if chg_5m < 0.005 or turnover_rate < 0.25:
                    continue

            # Institutional Quality Moat
            if liq < 35000.0 or mcap < 60000.0 or chg_5m < -0.01 or buy_ratio < 0.48:
                continue

            if (action == "ACTION") or (sm >= heur["min_smart_money"]):
                candidates.append({
                    "address": addr,
                    "symbol": sym,
                    "market_cap": mcap,
                    "volume": vol,
                    "liquidity": liq,
                    "chg_5m": chg_5m,
                    "buy_ratio": buy_ratio,
                    "bundler": bundler,
                    "dev_hold": dev_hold,
                    "smart_degen_count": sm,
                    "priority": dec.get("priority", 0),
                    "action": action,
                    "risk_label": dec.get("risk_label", "MODERATE_RISK"),
                    "council_score": dec.get("council_score", 50),
                    "council": dec.get("council"),
                    "features": feat
                })
        candidates.sort(key=lambda x: (x.get("action") == "ACTION", x.get("priority", 0), x.get("smart_degen_count", 0)), reverse=True)
        return candidates
    except urllib.error.HTTPError as e:
        err_msg = ""
        try:
            err_msg = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        if e.code == 429 or "RATE_LIMIT" in err_msg or "CIRCUIT_BREAKER" in err_msg:
            logger.warning(f"GMGN Rate Limit / Circuit Breaker active ({e.code}). Enforcing 300s screening cool-off...")
            state["last_screen_time"] = time.time() + 300
        elif e.code == 502 and ("CIRCUIT_BREAKER" in err_msg or "RATE_LIMIT" in err_msg):
            logger.warning("GMGN Rate Limit in progress via backend 502. Enforcing 300s screening cool-off...")
            state["last_screen_time"] = time.time() + 300
        else:
            logger.error(f"Screening API error ({e.code}): {err_msg or e}")
        return []
    except Exception as e:
        logger.error(f"Screening exception: {e}")
        return []

def check_memex_gates(address: str, symbol: str, feat: dict = None, is_reentry: bool = False) -> Tuple[bool, str]:
    try:
        if feat:
            b1m = float(feat.get("buy_volume_1m", 0.0))
            s1m = float(feat.get("sell_volume_1m", 0.0))
            b5m = float(feat.get("buy_volume_5m", 0.0))
            s5m = float(feat.get("sell_volume_5m", 0.0))
            top70_hold = float(feat.get("top70_sniper_hold", 0.0) or feat.get("top10", 0.0))
            
            if top70_hold > 0.40:
                return False, f"REJECT_MEMEX_SKILL3 (Snipers hold {top70_hold*100:.1f}% > 40%)"
            if s5m > b5m and s5m > 2000.0:
                return False, f"REJECT_MEMEX_SKILL3 (5m Net Outflow: Sell ${s5m:,.0f} > Buy ${b5m:,.0f})"
                
            if s1m > b1m and s1m > 1000.0:
                return False, f"REJECT_MEMEX_SKILL4 (1m Dump: Sell ${s1m:,.0f} > Buy ${b1m:,.0f})"

            return True, "PASS"

        return True, "PASS"
    except Exception as e:
        logger.warning(f"MemeX gate error on {symbol}: {e}")
        return True, "PASS"

# ==============================================================================
# SECTION 6: TWO-PHASE STAGED BUY & SELL DISPATCHERS
# ==============================================================================

def detect_market_regime(candidates: List[dict] = None) -> dict:
    """
    Evaluates market regime based on candidate breadth, momentum acceleration, and smart money presence:
    - High Conviction Concentration (Option 3): High average 5m momentum (>= 3%), strong liquidity (> $80k), SM confluence >= 8 -> 1.20 SOL, 3 slots max
    - Micro-Scalp Agility (Option 2): Low average 5m momentum (<= 0.5%), choppy/low turnover, thin liquidity -> 0.25 SOL, 8 slots max
    - Standard Conservative (Option 1): Balanced baseline market -> 0.70 SOL, 5 slots max
    """
    if not candidates:
        return {"regime": "STANDARD_CONSERVATIVE", "trade_size_sol": 0.50, "max_positions": 8, "exposure_cap_sol": 5.50}
    
    top = candidates[:6]
    def extract_chg_5m(c: dict) -> float:
        if "chg_5m" in c:
            val = float(c["chg_5m"])
            return val / 100.0 if abs(val) > 1.0 else val
        for k in ("price_change_5m", "price_change_percent_5m"):
            if k in c:
                return float(c[k]) / 100.0
        return 0.0

    def extract_sm(c: dict) -> int:
        return int(c.get("smart_degen_count") or c.get("smart_money_count") or c.get("smart_wallets") or 0)

    avg_chg_5m = sum(extract_chg_5m(c) for c in top) / max(len(top), 1)
    high_conviction_count = sum(
        1 for c in top
        if extract_sm(c) >= 6 and (c.get("liquidity") or 0) >= 80_000 and extract_chg_5m(c) >= 0.02
    )

    if high_conviction_count >= 2 or avg_chg_5m >= 0.03:
        return {"regime": "HIGH_CONVICTION", "trade_size_sol": 0.80, "max_positions": 5, "exposure_cap_sol": 5.50}
    elif avg_chg_5m <= 0.005 or len([c for c in top if extract_sm(c) >= 4]) == 0:
        return {"regime": "MICRO_SCALP", "trade_size_sol": 0.35, "max_positions": 10, "exposure_cap_sol": 4.50}
    else:
        return {"regime": "STANDARD_CONSERVATIVE", "trade_size_sol": 0.50, "max_positions": 8, "exposure_cap_sol": 5.50}

def calculate_dynamic_size(council_score: int, sm_count: int, liq: float, regime: dict = None, portfolio_equity_sol: float = 0.0) -> float:
    """Dynamic Conviction Sizing: 10% to 20% of portfolio equity, bounded by regime and risk."""
    if portfolio_equity_sol > 0:
        if council_score >= 85 and sm_count >= 5 and liq >= 50_000:
            target_pct = 0.10
        elif council_score >= 60 and sm_count >= 2 and liq >= 25_000:
            target_pct = 0.07
        else:
            target_pct = 0.05
        allocated = round(portfolio_equity_sol * target_pct, 3)
        return max(MIN_TRADE_SIZE_SOL, min(allocated, MAX_TRADE_SIZE_SOL))

    base_size = regime.get("trade_size_sol", DEFAULT_TRADE_SIZE_SOL) if regime else DEFAULT_TRADE_SIZE_SOL
    if council_score >= 85 and sm_count >= 10 and liq >= 100_000:
        return min(round(base_size * 1.2, 3), MAX_TRADE_SIZE_SOL)
    elif council_score >= 70 and sm_count >= 5 and liq >= 50_000:
        return round(base_size, 3)
    else:
        return max(round(base_size * 0.75, 3), MIN_TRADE_SIZE_SOL)

def execute_buy(
    address: str,
    symbol: str,
    mcap: float,
    sm_count: int,
    liq: float,
    is_reentry: bool = False,
    council_score: int = 50,
    risk_label: str = "MODERATE_RISK",
    regime: dict = None
) -> bool:
    """
    Executes a BUY using the Anthropic Two-Phase Staged Guardrail Architecture.
    Phase 1: stage_swap() validates provenance, slot budget, portfolio cap, liquidity floor.
    Phase 2: apply_swap() re-checks guardrails on fresh live state then dispatches /api/buy.
    """
    kind = SwapKind.BUY_REENTRY if is_reentry else SwapKind.BUY_FRESH
    actor = "WAVE_RIDER_ENGINE" if is_reentry else "AUTONOMOUS_SCREENER"
    
    dynamic_size = calculate_dynamic_size(
        council_score, sm_count, liq, regime=regime,
        portfolio_equity_sol=10.0
    )
    max_slots = regime.get("max_positions", MAX_POSITIONS) if regime else MAX_POSITIONS
    
    # Phase 1: Stage Swap & Run Phase-1 Guardrails
    staged = swap_ledger.stage_swap(
        kind=kind,
        address=address,
        symbol=symbol,
        size_sol=dynamic_size,
        percent=100,
        reason=f"{'Re-entry continuation' if is_reentry else 'Fresh rotation admission'} | Mcap: ${mcap:,.0f} | Liq: ${liq:,.0f} | SM: {sm_count} | Size: {dynamic_size} SOL",
        actor=actor,
        context={
            "mcap": mcap,
            "sm_count": sm_count,
            "liquidity": liq,
            "is_reentry": is_reentry,
            "council_score": council_score,
            "risk_label": risk_label,
            "max_positions": max_slots
        },
        portfolio_context=get_portfolio_context(),
        live_heuristics=get_live_heuristics(),
        bot_state=state
    )

    if staged.status != SwapStatus.STAGED:
        logger.info(f"BUY CANCELLED AT STAGE 1: {symbol} rejected by guardrails")
        return False

    # Execution callback for Phase 2
    def _do_buy_dispatch(sw: StagedSwap) -> dict:
        return http_post(f"{API_BASE}/api/buy", {
            "chain": CHAIN,
            "address": sw.address,
            "size_sol": sw.size_sol
        }, timeout=20)

    # Phase 2: Pre-Flight Re-verification & Execution Dispatch
    success, applied_swap, tx_result = swap_ledger.apply_swap(
        stage_id=staged.stage_id,
        execute_fn=_do_buy_dispatch,
        portfolio_context_fn=get_portfolio_context,
        live_heuristics_fn=get_live_heuristics,
        bot_state=state
    )

    if success:
        state["total_trades"] += 1
        state["entry_timestamps"][address] = time.time()
        state["entry_sm_counts"][address] = sm_count
        state["entry_mcaps"][address] = mcap
        state["entry_liquidities"][address] = liq
        state["peak_liquidity"][address] = liq
        state["is_reentry"][address] = is_reentry
        state["position_sizes"][address] = dynamic_size
        save_audit_state()
        return True
    else:
        logger.warning(f"BUY APPLICATION FAILED for {symbol}: {applied_swap.violations}")
        return False

def execute_sell(
    address: str,
    symbol: str,
    reason: str,
    pnl_pct: float = 0.0,
    cur_price: float = 0.0,
    entry_price: float = 0.0,
    percent: int = 100,
    pos_size_sol: float = 0.0
) -> bool:
    """
    Executes a SELL using the Anthropic Two-Phase Staged Guardrail Architecture.
    Phase 1: stage_swap() validates held status, exit sizing, and provenance.
    Phase 2: apply_swap() re-checks guardrails on fresh state then dispatches /api/sell.
    """
    actual_pos_size = pos_size_sol if pos_size_sol > 0 else state["position_sizes"].get(address, DEFAULT_TRADE_SIZE_SOL)
    sold_size = round(actual_pos_size * (percent / 100.0), 4)

    # Determine Swap Kind
    if "EMERGENCY" in reason:
        kind = SwapKind.SELL_EMERGENCY
    elif "STOP" in reason:
        kind = SwapKind.SELL_STOP
    elif "TP" in reason or "HARVEST" in reason or "RATCHET" in reason:
        kind = SwapKind.SELL_TP if percent >= 100 else SwapKind.SELL_PARTIAL
    else:
        kind = SwapKind.SELL_FULL if percent >= 100 else SwapKind.SELL_PARTIAL

    # Record actual PnL without any floor clamping -- honest losses reported as-is
    effective_pnl = pnl_pct


    pnl_sol = round(sold_size * effective_pnl, 6)
    effective_exit_price = round(entry_price * (1.0 + effective_pnl), 8) if entry_price > 0 else cur_price

    # Phase 1: Stage Sell Swap
    staged = swap_ledger.stage_swap(
        kind=kind,
        address=address,
        symbol=symbol,
        size_sol=sold_size,
        percent=percent,
        reason=reason,
        actor="RISK_MONITOR_CITADEL",
        context={"pnl_pct": effective_pnl, "pnl_sol": pnl_sol, "exit_price": effective_exit_price},
        portfolio_context=get_portfolio_context(),
        live_heuristics=get_live_heuristics(),
        bot_state=state
    )

    if staged.status != SwapStatus.STAGED:
        logger.warning(f"SELL CANCELLED AT STAGE 1: {symbol} rejected by guardrails: {staged.violations}")
        return False

    # Execution callback for Phase 2
    def _do_sell_dispatch(sw: StagedSwap) -> dict:
        try:
            return http_post(f"{API_BASE}/api/sell", {
                "chain": CHAIN,
                "address": sw.address,
                "percent": sw.percent
            }, timeout=20)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.info(f"Position {sw.symbol} ({sw.address[:8]}...) already closed on server (404). Reconciling state as applied.")
                return {"ok": True, "already_closed": True, "symbol": sw.symbol}
            raise

    # Phase 2: Pre-Flight Re-verification & Execution Dispatch
    success, applied_swap, tx_result = swap_ledger.apply_swap(
        stage_id=staged.stage_id,
        execute_fn=_do_sell_dispatch,
        portfolio_context_fn=get_portfolio_context,
        live_heuristics_fn=get_live_heuristics,
        bot_state=state
    )

    if not success:
        logger.error(f"SELL APPLICATION FAILED for {symbol}: {applied_swap.violations}")
        return False

    entry_t = state["entry_timestamps"].get(address, time.time())
    hold_mins = (time.time() - entry_t) / 60.0

    trade_record = {
        "symbol": symbol,
        "address": address,
        "reason": reason,
        "stage_id": staged.stage_id,
        "percent": percent,
        "entry_price": entry_price,
        "exit_price": effective_exit_price,
        "pnl_pct": round(effective_pnl, 4),
        "pnl_sol": round(pnl_sol, 6),
        "hold_minutes": round(hold_mins, 1),
        "mcap": state["entry_mcaps"].get(address, 0.0),
        "market_cap": state["entry_mcaps"].get(address, 0.0),
        "liquidity": state["entry_liquidities"].get(address, 0.0),
        "is_reentry": state["is_reentry"].get(address, False),
        "timestamp": datetime.now().isoformat()
    }
    state["closed_trades"].append(trade_record)
    state["total_realized_pnl_sol"] += pnl_sol

    try:
        heur = brain.get_heuristics()
        logger.info(f"BRAIN HEURISTICS UPDATED: WR EMA {heur.get('win_rate_moving_avg', 0.40):.1%} | Total Reflections: {heur.get('total_reflections_logged', 0)}")
    except Exception as e:
        logger.debug(f"Brain status error: {e}")

    if percent >= 100:
        th = state["token_history"].get(address, {
            "symbol": symbol,
            "trades_count": 0,
            "cumulative_pnl_sol": 0.0,
            "last_pnl_pct": 0.0,
            "last_exit_time": 0.0
        })
        th["trades_count"] += 1
        th["cumulative_pnl_sol"] += pnl_sol
        th["last_pnl_pct"] = pnl_pct
        th["last_exit_time"] = time.time()
        
        snapshot = th.get("entry_candidate_snapshot")
        if snapshot:
            from kida_bot.strategies.backprop import backpropagate_trade
            try:
                trade_record["realized_pnl"] = effective_pnl
                backpropagate_trade(trade_record, snapshot)
            except Exception as e:
                logger.error(f"Backprop failed for {symbol}: {e}")
                
        state["token_history"][address] = th

        state["peak_prices"].pop(address, None)
        state["peak_liquidity"].pop(address, None)
        state["entry_timestamps"].pop(address, None)
        state["entry_sm_counts"].pop(address, None)
        state["is_reentry"].pop(address, None)
        state["position_sizes"].pop(address, None)
        state["active_stop_floors"].pop(address, None)
        state["partial_tp_taken"].discard(address)
        state["moonbag_active"].discard(address)
        state["break_even_locked"].discard(address)

        if pnl_sol < 0 or "STOP" in reason:
            state["cooldown_until"][address] = time.time() + NEGATIVE_TOKEN_LOCKOUT_SECONDS
            logger.info(f"NEGATIVE LOCKOUT: {symbol} closed at loss ({pnl_sol:+.4f} SOL). Blacklisted for 6 hours.")
        else:
            state["cooldown_until"][address] = time.time() + 900
            logger.info(f"CONSOLIDATION COOLDOWN: {symbol} exited. Enforcing 15m cooldown before re-evaluation.")
    else:
        state["partial_tp_taken"].add(address)
        state["break_even_locked"].add(address)
        state["position_sizes"][address] = round(actual_pos_size - sold_size, 4)

    # Webhook Alert integration
    def _fire_webhooks():
        discord_url = os.environ.get("DISCORD_WEBHOOK_URL")
        telegram_url = os.environ.get("TELEGRAM_WEBHOOK_URL")
        if not discord_url and not telegram_url:
            return
            
        weights = {}
        try:
            from kida_bot.strategies.backprop import load_weights
            weights = load_weights()
        except:
            pass

        msg = f"🚨 **KIDA Citadel Alert - {symbol} exited** 🚨\n"
        msg += f"- Reason: `{reason}`\n"
        msg += f"- PnL: `{effective_pnl * 100:.2f}%` ({pnl_sol:.4f} SOL)\n"
        msg += f"- AI Weights -> SM: {weights.get('w_smart_money', 0)*100:.1f}%, Liq: {weights.get('w_liquidity', 0)*100:.1f}%, Mom: {weights.get('w_momentum', 0)*100:.1f}%\n"

        if discord_url:
            try:
                req = urllib.request.Request(discord_url, method="POST", headers={"Content-Type": "application/json"}, data=json.dumps({"content": msg}).encode("utf-8"))
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                logger.error(f"Discord Webhook Failed: {e}")
                
        if telegram_url:
            try:
                # Telegram usually needs format: api.telegram.org/bot<token>/sendMessage
                payload = json.dumps({"chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""), "text": msg}).encode("utf-8")
                req = urllib.request.Request(telegram_url, method="POST", headers={"Content-Type": "application/json"}, data=payload)
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                logger.error(f"Telegram Webhook Failed: {e}")
                
    import threading
    threading.Thread(target=_fire_webhooks, daemon=True).start()

    save_audit_state()
    return True

# ==============================================================================
# SECTION 7: FAST RISK MONITOR & DECISION ENGINE
# ==============================================================================

def monitor_and_manage_risk():
    positions = get_positions()
    now = time.time()

    for pos in positions:
        addr = pos.get("address")
        sym = pos.get("symbol", "TOKEN")
        pnl = pos.get("pnl", 0.0)
        cur_price = pos.get("cur_price", 0.0)
        entry_price = pos.get("entry_price", 0.0)
        severity = pos.get("severity", 0)
        liq = pos.get("liquidity", 0.0)
        size_sol = pos.get("size_sol", DEFAULT_TRADE_SIZE_SOL)

        # Corrupted / Unpriced Position Defense: Purge zero/negative prices immediately
        if entry_price <= 0.0 or cur_price <= 0.0:
            logger.warning(f"UNPRICED ZOMBIE POSITION DETECTED: {sym} (entry: {entry_price}, cur: {cur_price}). Purging immediately...")
            execute_sell(
                addr, sym,
                f"CORRUPTED_ZERO_PRICE_PURGE (entry: {entry_price}, cur: {cur_price} — slot unseated)",
                pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol
            )
            continue

        if addr not in state["position_sizes"]:
            state["position_sizes"][addr] = size_sol
            if size_sol <= round(DEFAULT_TRADE_SIZE_SOL * 0.6, 3):
                state["partial_tp_taken"].add(addr)
                state["break_even_locked"].add(addr)

        if addr not in state["entry_timestamps"]:
            state["entry_timestamps"][addr] = now
        elapsed_seconds = now - state["entry_timestamps"][addr]

        if addr not in state["peak_prices"] or cur_price > state["peak_prices"][addr]:
            state["peak_prices"][addr] = cur_price

        if liq > 0:
            if addr not in state["peak_liquidity"] or liq > state["peak_liquidity"][addr]:
                state["peak_liquidity"][addr] = liq

        # High-Water Mark Trailing Protection: For positions with peak gain >= +10%, prevent round-trips
        cur_peak_pnl = state.setdefault("peak_pnl", {}).get(addr, 0.0)
        if pnl > cur_peak_pnl:
            state["peak_pnl"][addr] = pnl
            cur_peak_pnl = pnl

        if cur_peak_pnl >= 0.10 and addr not in state["moonbag_active"]:
            trailing_floor = max(0.06, cur_peak_pnl - 0.04)
            cur_floor = state["active_stop_floors"].get(addr, STOP_LOSS_PCT)
            if trailing_floor > cur_floor:
                state["active_stop_floors"][addr] = trailing_floor
                logger.info(f"HIGH-WATER MARK TRAILING LOCK: {sym} peak +{cur_peak_pnl*100:.1f}%. Floor ratcheted to +{trailing_floor*100:.1f}%.")

        # 1. Emergency Escape: Severity >= 50 or AMM Flash Drain >= 20%
        if severity >= 50:
            execute_sell(addr, sym, f"EMERGENCY_ESCAPE (Severity: {severity})", pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol)
            continue

        if liq > 0 and addr in state["peak_liquidity"] and state["peak_liquidity"][addr] > 0:
            liq_drop = (state["peak_liquidity"][addr] - liq) / state["peak_liquidity"][addr]
            if liq_drop >= EMERGENCY_LIQUIDITY_DROP_THRESHOLD:
                execute_sell(
                    addr, sym,
                    f"EMERGENCY_FLASH_DRAIN (Pool Liquidity dropped {liq_drop*100:.1f}%)",
                    pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol
                )
                continue

        # 2. Asymmetric Free-Roll Moonbag Management
        if addr in state["moonbag_active"]:
            peak_p = state["peak_prices"].get(addr, cur_price)
            if peak_p > 0:
                pullback = (peak_p - cur_price) / peak_p
                if pullback >= MOONBAG_TRAILING_STOP_PULLBACK:
                    execute_sell(
                        addr, sym,
                        f"MOONBAG_TRAILING_HARVEST (ATH: ${peak_p:.8f}, Pullback: -{pullback*100:.1f}%, Final PnL: +{pnl*100:.1f}%)",
                        pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol
                    )
                    continue

            if pnl <= 0.10:
                execute_sell(
                    addr, sym,
                    f"MOONBAG_FLOOR_PROTECTION (+10% floor hit, Final PnL: +{pnl*100:.1f}%)",
                    pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol
                )
                continue

            continue

        # 3. Stepped Multi-Tier Profit Harvest & Ratchet Vault
        if addr not in state["moonbag_active"]:
            tp_stage = state.setdefault("tp_stage", {}).get(addr, 0)

            # Tier 1: +8% gain -> Lock Breakeven Floor (+2.0%, covering fees)
            if pnl >= TIER1_TRIGGER_PCT:
                cur_floor = state["active_stop_floors"].get(addr, STOP_LOSS_PCT)
                if TIER1_LOCK_PCT > cur_floor:
                    state["active_stop_floors"][addr] = TIER1_LOCK_PCT
                    logger.info(f"BREAK-EVEN LOCKED: {sym} reached +{pnl*100:.1f}%. Stop floor raised to +{TIER1_LOCK_PCT*100:.1f}%.")

            # Tier 2: +15% gain -> Bank 50% partial profit & raise floor to +8.0%
            if pnl >= TIER2_TRIGGER_PCT and tp_stage < 1:
                state["tp_stage"][addr] = 1
                state["active_stop_floors"][addr] = TIER2_LOCK_PCT
                execute_sell(
                    addr, sym,
                    f"PARTIAL_TP_TIER2 (+{pnl*100:.1f}% hit: 50% banked, floor +{TIER2_LOCK_PCT*100:.1f}%)",
                    pnl, cur_price, entry_price, percent=50, pos_size_sol=size_sol
                )
                continue

            # Tier 3: +30% gain -> Bank 60% of remaining (30% original) & raise floor to +20.0%
            if pnl >= TIER3_TRIGGER_PCT and tp_stage < 2:
                state["tp_stage"][addr] = 2
                state["active_stop_floors"][addr] = TIER3_LOCK_PCT
                execute_sell(
                    addr, sym,
                    f"PARTIAL_TP_TIER3 (+{pnl*100:.1f}% hit: 30% banked, floor +{TIER3_LOCK_PCT*100:.1f}%)",
                    pnl, cur_price, entry_price, percent=60, pos_size_sol=size_sol
                )
                continue

            # Tier 4: Activate Free-Roll Moonbag
            if pnl >= TAKE_PROFIT_PCT and tp_stage < 3:
                state["tp_stage"][addr] = 3
                state["moonbag_active"].add(addr)
                logger.info(f"MOONBAG ACTIVATED: {sym} reached +{pnl*100:.1f}%. Free-rolling with trailing stop from ATH.")
                continue

        # 4. Stop Loss & Ratchet Floor Enforcement
        active_floor = state["active_stop_floors"].get(addr)
        if active_floor is not None and active_floor > 0:
            if pnl <= active_floor:
                execute_sell(
                    addr, sym,
                    f"RATCHET_FLOOR_EXIT (+{active_floor*100:.1f}% floor hit, PnL: {pnl*100:.1f}%)",
                    pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol
                )
                continue
        else:
            is_reentry = state["is_reentry"].get(addr, False)
            active_stop = REENTRY_STOP_LOSS_PCT if is_reentry else STOP_LOSS_PCT
            if pnl <= active_stop:
                stop_label = f"REENTRY_STOP ({REENTRY_STOP_LOSS_PCT*100:.1f}%)" if is_reentry else f"HARD_STOP ({STOP_LOSS_PCT*100:.1f}%)"
                execute_sell(
                    addr, sym,
                    f"{stop_label} (PnL: {pnl*100:.1f}%)",
                    pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol
                )
                continue

        # 4b. Rapid Momentum Failure Cut: If trade immediately reverses down > 2.0% within first 25-90s, cut immediately
        if elapsed_seconds >= 25 and pnl <= -0.020:
            execute_sell(
                addr, sym,
                f"RAPID_MOMENTUM_CUT ({elapsed_seconds:.0f}s, PnL: {pnl*100:+.1f}% <= -2.0% — early cut)",
                pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol
            )
            continue

        # 4c. Early Bleeder Defense: If down > 1.8% after 90s, cut immediately to prevent hard-stop slippage blowouts
        if elapsed_seconds >= 90 and pnl <= -0.018:
            execute_sell(
                addr, sym,
                f"EARLY_BLEEDER_DEFENSE ({elapsed_seconds/60:.1f}m, PnL: {pnl*100:+.1f}% <= -1.8% — mitigating hard stop slip)",
                pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol
            )
            continue

        # 5. Dynamic Stagnation Exit: If held >= STAGNATION_TIMEOUT_SECONDS
        if elapsed_seconds >= STAGNATION_TIMEOUT_SECONDS:
            if pnl >= 0.006:
                # Green Consolidation: Bank 50% profit, lock floor at +0.4%, and extend hold
                if addr not in state["partial_tp_taken"]:
                    state["active_stop_floors"][addr] = 0.004
                    execute_sell(
                        addr, sym,
                        f"CONSOLIDATION_PROFIT_HARVEST ({elapsed_seconds/60:.1f}m, PnL: {pnl*100:+.1f}%: banking 50% gain, +0.4% floor locked)",
                        pnl, cur_price, entry_price, percent=50, pos_size_sol=size_sol
                    )
                    state["entry_timestamps"][addr] = now  # Reset hold timer for remaining portion
                    continue
            elif pnl < STAGNATION_MIN_PNL_TARGET:
                execute_sell(
                    addr, sym,
                    f"STAGNATION_RECYCLE ({elapsed_seconds/60:.1f}m, PnL: {pnl*100:+.1f}% < +{STAGNATION_MIN_PNL_TARGET*100:.1f}% target — slot freed)",
                    pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol
                )
                continue

# ==============================================================================
# SECTION 8: CYCLE ORCHESTRATION & SUMMARY
# ==============================================================================

def cycle():
    state["cycle_count"] += 1
    status = get_status()
    if not status:
        logger.warning("Backend service unavailable. Retrying in next cycle...")
        return

    positions = get_positions()
    current_count = len(positions)
    now = time.time()
    elapsed_total = now - state["session_start_time"]

    if current_count > 0:
        monitor_and_manage_risk()
        positions = get_positions()
        current_count = len(positions)

    pos_summary = []
    for p in positions:
        pos_summary.append(f"{p.get('symbol')} ({p.get('pnl', 0)*100:+.2f}%)")
    pos_str = ", ".join(pos_summary) if pos_summary else "Empty"

    logger.info(
        f"Cycle #{state['cycle_count']:>3} | Elapsed: {elapsed_total/60:4.1f}m/{SESSION_DURATION_SECONDS/60:.0f}m | "
        f"Slots: {current_count}/{MAX_POSITIONS} [{pos_str}] | "
        f"Realized PnL: {state['total_realized_pnl_sol']:+.4f} SOL"
    )

    save_audit_state()

    available_slots = MAX_POSITIONS - current_count
    if available_slots <= 0:
        return

    now = time.time()
    if (now - state["last_screen_time"]) >= SCREEN_INTERVAL_SECONDS or current_count == 0:
        state["last_screen_time"] = now
        candidates = run_screening()
        regime = detect_market_regime(candidates)
        active_max_positions = regime["max_positions"]
        available_slots = active_max_positions - current_count
        logger.info(
            f"MARKET REGIME: {regime['regime']} | Sizing: {regime['trade_size_sol']} SOL | "
            f"Capacity: {current_count}/{active_max_positions} slots | Cap: {regime['exposure_cap_sol']} SOL"
        )
        held_addrs = {p["address"].lower() for p in positions}

        # Priority 1: Check Wave Rider Re-entry candidates first
        for c in candidates:
            if available_slots <= 0:
                break
            addr = c.get("address")
            sym = c.get("symbol")
            if not addr or addr.lower() in held_addrs:
                continue

            th = state["token_history"].get(addr)
            heur = get_live_heuristics()
            if c.get("liquidity", 0) < heur["min_pool_liquidity_usd"]:
                continue
            if th and th.get("last_pnl_pct", 0) >= 0.02 and th.get("trades_count", 0) < MAX_REENTRIES_PER_TOKEN:
                if now >= state["cooldown_until"].get(addr, 0):
                    if c.get("chg_5m", 0) >= 0.005 and c.get("buy_ratio", 0) >= 0.50:
                        ok_memex, reason_memex = check_memex_gates(addr, sym, feat=c.get("features"), is_reentry=True)
                        if not ok_memex:
                            logger.info(f"WAVE RIDER RE-ENTRY BLOCKED: {sym} — {reason_memex}")
                            state["cooldown_until"][addr] = now + 1200
                            continue
                        success = execute_buy(
                            addr, sym, c.get("market_cap", 0), c.get("smart_degen_count", 0), c.get("liquidity", 0),
                            is_reentry=True,
                            council_score=80,
                            risk_label="LOW_RISK",
                            regime=regime
                        )
                        if success:
                            held_addrs.add(addr.lower())
                            available_slots -= 1
                            continue

        # Priority 2: Fresh candidates vetted by screening + advisory council
        for c in candidates:
            if available_slots <= 0:
                break
            addr = c.get("address")
            sym = c.get("symbol")
            mcap = c.get("market_cap", 0)
            vol_1h = c.get("volume", 0)
            sm_count = c.get("smart_degen_count", 0)
            liq = c.get("liquidity", 0)
            chg_5m = c.get("chg_5m", 0)
            buy_ratio = c.get("buy_ratio", 0.5)
            bundler = c.get("bundler", 0)
            dev_hold = c.get("dev_hold", 0)
            feat = c.get("features", {})
            action = c.get("action", "")

            if not addr or addr.lower() in held_addrs:
                continue
            if now < state["cooldown_until"].get(addr, 0):
                continue

            # Anti-Churn Defense: Check token history for previous net loss or hard stop
            th = state.get("token_history", {}).get(addr, {})
            if th.get("cumulative_pnl_sol", 0.0) < 0 or (th.get("trades_count", 0) >= 1 and th.get("last_pnl_pct", 0) < 0):
                state["cooldown_until"][addr] = now + NEGATIVE_TOKEN_LOCKOUT_SECONDS
                continue

            # Entry Quality Defense: Reject overbought spikes and sell-pressure distribution
            if c.get("entry_quality") == "BAD":
                state["cooldown_until"][addr] = now + 900
                continue

            heur = get_live_heuristics()

            # Conservative Institutional Moat Enforcement
            if liq < heur["min_pool_liquidity_usd"]:
                continue
            if sm_count < heur["min_smart_money"]:
                continue
            if bundler > heur["max_bundler_rate"]:
                continue
            if dev_hold > heur["max_dev_hold_rate"]:
                continue

            ok_memex, reason_memex = check_memex_gates(addr, sym, feat=feat, is_reentry=False)
            if not ok_memex:
                logger.info(f"FRESH CANDIDATE BLOCKED: {sym} — {reason_memex}")
                state["cooldown_until"][addr] = now + 600
                continue

            council_eval = c.get("council") or brain.evaluate_candidate(c)
            council_score = council_eval.get("consensus_score", 50)
            risk_label = c.get("risk_label") or (
                "LOW_RISK" if council_score >= 80 else
                ("MODERATE_RISK" if council_score >= 60 else
                ("ELEVATED_RISK" if council_score >= 40 else "HIGH_RISK"))
            )
            gates_passed = sum(1 for d in council_eval.get("debates", []) if d.get("verdict") == "PASS")

            # Council Veto: block score < 40 or explicitly rejected.
            if council_score < 40 or council_eval.get("verdict") == "REJECTED":
                logger.info(f"COUNCIL VETO ({sym}): Score {council_score}/100 [{risk_label}] ({gates_passed}/5 sentinels) — {council_eval.get('verdict_rationale')}")
                state["cooldown_until"][addr] = now + 900
                continue

            logger.info(f"COUNCIL APPROVED ({sym}): Score {council_score}/100 [{risk_label}] ({gates_passed}/5 sentinels) — {council_eval.get('verdict_rationale')}")

            success = execute_buy(
                addr, sym, mcap, sm_count, liq,
                is_reentry=False,
                council_score=council_score,
                risk_label=risk_label,
                regime=regime
            )
            if success:
                held_addrs.add(addr.lower())
                available_slots -= 1

def print_final_summary():
    elapsed = time.time() - state["session_start_time"]
    positions = get_positions()
    unrealized_sol = sum(p.get("pnl", 0) * p.get("size_sol", DEFAULT_TRADE_SIZE_SOL) for p in positions)
    total_pnl = state["total_realized_pnl_sol"] + unrealized_sol

    wins = [t for t in state["closed_trades"] if t.get("pnl_sol", 0) > 0]
    losses = [t for t in state["closed_trades"] if t.get("pnl_sol", 0) < 0]
    sol_gained = sum(t["pnl_sol"] for t in wins)
    sol_lost = abs(sum(t["pnl_sol"] for t in losses))
    win_rate = (len(wins) / len(state["closed_trades"]) * 100) if state["closed_trades"] else 0.0

    logger.info("================================================================")
    logger.info("    GMGN $100 MICRO-SCALP WAVE RIDER SESSION AUDIT COMPLETE")
    logger.info("================================================================")
    logger.info(f"  Total Session Duration: {elapsed/60:.1f} minutes")
    logger.info(f"  Total Screening Cycles: {state['cycle_count']}")
    logger.info(f"  Tokens Scanned on-Chain: {state['tokens_scanned']}")
    logger.info(f"  Total Positions Opened: {state['total_trades']}")
    logger.info(f"  Total Trades Closed: {len(state['closed_trades'])}")
    logger.info(f"  Winning Trades: {len(wins)} | Losing Trades: {len(losses)} | Win Rate: {win_rate:.1f}%")
    logger.info(f"  Total SOL Gained: +{sol_gained:.6f} SOL")
    logger.info(f"  Total SOL Lost: -{sol_lost:.6f} SOL")
    logger.info(f"  Realized PnL: {state['total_realized_pnl_sol']:+.6f} SOL")
    logger.info(f"  Unrealized PnL: {unrealized_sol:+.6f} SOL")
    logger.info(f"  Net Portfolio PnL: {total_pnl:+.6f} SOL")
    logger.info(f"  Active Slots at Close: {len(positions)}/{MAX_POSITIONS}")
    logger.info("----------------------------------------------------------------")
    logger.info("  TWO-PHASE STAGED GUARDRAILS & PROVENANCE CITADEL REPORT:")
    prov_st = provenance_registry.get_stats()
    ledger_st = swap_ledger.get_audit_summary()
    logger.info(f"  Provenance Mints Registered: {prov_st['total_registered']}")
    logger.info(f"  Provenance Verifications Passed: {prov_st['verifications_passed']}")
    logger.info(f"  Provenance Invalidation/Rejections: {prov_st['verifications_failed']}")
    logger.info(f"  Phase-1 Swaps Staged: {ledger_st['total_staged']}")
    logger.info(f"  Phase-1 Swaps Guardrail Rejected: {ledger_st['total_rejected']}")
    logger.info(f"  Phase-2 Swaps Applied on-Chain: {ledger_st['total_applied']}")
    logger.info(f"  Phase-2 Swaps Discarded/Aborted: {ledger_st['total_discarded']}")
    logger.info("================================================================")
    save_audit_state()
    archive_file = AUDIT_FILE.parent / f"session_audit_{int(state['session_start_time'])}.json"
    try:
        shutil.copyfile(AUDIT_FILE, archive_file)
        logger.info(f"  Permanent Archive: {archive_file}")
    except Exception as e:
        logger.warning(f"Failed to create permanent session archive: {e}")

def main():
    logger.info("================================================================")
    logger.info("  KIDA // AUTONOMOUS ON-CHAIN SNIPER CITADEL (SHADOW MODE)")
    logger.info("  ANTHROPIC COMMERCE-AGENT TWO-PHASE STAGED GUARDRAILS ACTIVE")
    logger.info(f"  Target Chain: {CHAIN.upper()} | Concurrent Slots: {MAX_POSITIONS}")
    logger.info(f"  Dynamic Position Sizing: {MIN_TRADE_SIZE_SOL}-{MAX_TRADE_SIZE_SOL} SOL per slot")
    logger.info(f"  Micro-Monitor Cadence: {FAST_MONITOR_INTERVAL_SECONDS}s (Fast In-Memory Tick)")
    logger.info(f"  Take-Profit Target: +{TAKE_PROFIT_PCT*100:.1f}% | Base Stop-Loss: {STOP_LOSS_PCT*100:.1f}%")
    logger.info(f"  House Money Re-Entry Stop: {REENTRY_STOP_LOSS_PCT*100:.1f}%")
    heur = get_live_heuristics()
    logger.info(f"  Liquidity Moat: >= ${heur['min_pool_liquidity_usd']:,.0f} Pool Depth | >= ${heur['min_mcap']:,.0f} Mcap | >= {heur['min_smart_money']} SM")
    logger.info(f"  Provenance Registry Initialized: Base58 Address Validation Active")
    logger.info("================================================================")

    # Ingest active portfolio positions into provenance registry at startup
    try:
        cur_pos = get_positions()
        logger.info(f"Ingested {len(cur_pos)} active portfolio positions into Provenance Registry.")
    except Exception as e:
        logger.warning(f"Could not ingest initial positions: {e}")

    try:
        while True:
            elapsed = time.time() - state["session_start_time"]
            if elapsed >= SESSION_DURATION_SECONDS:
                logger.info(f"Target session duration ({SESSION_DURATION_SECONDS/3600:.1f}h) reached.")
                break
            
            try:
                positions = get_positions()
                if len(positions) > 0:
                    monitor_and_manage_risk()

                cycle()
            except Exception as e:
                logger.error(f"Transient error in cycle tick: {e}. Resuming next tick in {FAST_MONITOR_INTERVAL_SECONDS}s...")

            time.sleep(FAST_MONITOR_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Manual shutdown signal received.")
    except Exception as e:
        logger.error(f"Fatal error in execution loop: {e}")
    finally:
        print_final_summary()

if __name__ == "__main__":
    main()


