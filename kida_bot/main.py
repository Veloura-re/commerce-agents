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
from kida_bot.core.network import http_get, http_post, send_webhook_alert
from kida_bot.core.guardrails import *
from kida_bot.strategies.conviction import *
def get_status():
    try:
        return http_get(f"{API_BASE}/api/status", timeout=5)
    except Exception as e:
        logger.error(f"Failed to fetch API status: {e}")
        return None

def get_positions(retries: int = 3):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            data = http_get(f"{API_BASE}/api/positions?chain={CHAIN}", timeout=6)
            positions = data.get("positions", [])
            if positions is not None:
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
            last_err = e
            if attempt < retries:
                time.sleep(0.2 * (2 ** (attempt - 1)))

    # Secondary fallback to /api/state
    try:
        s_data = http_get(f"{API_BASE}/api/state", timeout=5)
        positions = s_data.get("active_positions", [])
        if positions:
            return positions
    except Exception:
        pass

    logger.error(f"Failed to fetch positions after {retries} attempts: {last_err}")
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
            "active_positions": positions,
            "candidates": state.get("last_candidates", [])
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

def _compute_entry_quality(chg_5m: float, chg_1h: float, buy_ratio: float) -> str:
    """
    Classify the current price entry as GOOD, NEUTRAL, or BAD.
    BAD = token already pumped hard / sellers taking over / dead cat bounce.
    GOOD = early momentum, buyers dominant, stable 1h trend.
    """
    bad_reasons = []
    good_signals = 0
    if chg_5m > 0.15:
        bad_reasons.append("OVERBOUGHT")
    elif chg_5m < 0.08:
        good_signals += 1
    if buy_ratio < 0.52:
        bad_reasons.append("SELL_PRESSURE")
    elif buy_ratio > 0.60:
        good_signals += 1
    if chg_1h < -0.05 and chg_5m > 0.08:
        bad_reasons.append("DEAD_CAT")
    if chg_1h > 0.05 and 0.01 < chg_5m < 0.15:
        good_signals += 1
    if bad_reasons:
        return "BAD"
    if good_signals >= 2:
        return "GOOD"
    return "NEUTRAL"


def run_screening():
    try:
        data = http_post(f"{API_BASE}/api/run", {"chain": CHAIN}, timeout=120)
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

            # High-Cadence Live Shadow Trading Moat
            if liq < 25_000.0 or mcap < 25_000.0 or chg_5m < -0.03 or buy_ratio < 0.40:
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
                    "price": dec.get("price") or feat.get("price", 0.0),
                    "priority": dec.get("priority", 0),
                    "action": action,
                    "risk_label": dec.get("risk_label", "MODERATE_RISK"),
                    "council_score": dec.get("council_score", 50),
                    "council": dec.get("council"),
                    "features": feat,
                    "entry_quality": _compute_entry_quality(chg_5m, feat.get("chg_1h", 0.0), buy_ratio),
                    "dex": dec.get("dex", "RAYDIUM"),
                    "top10": feat.get("top70_sniper_hold", 0.0)
                })
        # Also store ALL decisions (including rejected) as radar candidates
        state["last_candidates"] = [
            {
                "symbol": (item.get("decision") or {}).get("symbol") or (item.get("features") or {}).get("symbol", "TOKEN"),
                "address": (item.get("decision") or {}).get("address", ""),
                "dex": (item.get("decision") or {}).get("dex", "RAYDIUM"),
                "mcap": (item.get("decision") or {}).get("features", {}).get("mcap", 0),
                "liquidity": (item.get("decision") or {}).get("features", {}).get("liquidity", 0),
                "bundler": (item.get("decision") or {}).get("features", {}).get("bundler", 0),
                "dev_hold": (item.get("decision") or {}).get("features", {}).get("dev_hold", 0),
                "top10": (item.get("decision") or {}).get("features", {}).get("top70_sniper_hold", 0),
                "sm_confluence": (item.get("decision") or {}).get("features", {}).get("sm_confluence", 0),
                "chg_5m": (item.get("decision") or {}).get("features", {}).get("chg_5m", 0),
                "chg_1h": (item.get("decision") or {}).get("features", {}).get("chg_1h", 0),
                "action": (item.get("decision") or {}).get("action", "REJECT"),
                "rationale": (item.get("decision") or {}).get("reason", ""),
                "entry_quality": _compute_entry_quality(
                    (item.get("decision") or {}).get("features", {}).get("chg_5m", 0),
                    (item.get("decision") or {}).get("features", {}).get("chg_1h", 0),
                    (item.get("decision") or {}).get("features", {}).get("buy_ratio", 0.5)
                ),
                "council_score": (item.get("decision") or {}).get("council_score", 0),
                "risk_label": (item.get("decision") or {}).get("risk_label", "")
            }
            for item in decisions if (item.get("decision") or {}).get("address")
        ]
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

def execute_buy(
    address: str,
    symbol: str,
    mcap: float,
    sm_count: int,
    liq: float,
    is_reentry: bool = False,
    council_score: int = 50,
    risk_label: str = "MODERATE_RISK",
    regime: dict = None,
    price: float = 0.0
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
            "size_sol": sw.size_sol,
            "symbol": symbol,
            "price": price,
            "mcap": mcap,
            "liquidity": liq
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
    pos_size_sol: float = 0.0,
    slippage_through_stop: float = 0.0
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
                "percent": sw.percent,
                "reason": sw.reason
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

    from kida_bot.core.cost_model import calculate_net_pnl
    cost_info = calculate_net_pnl(sold_size, effective_pnl, fees_paid_sol=0.005)

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
        "gross_pnl_pct": cost_info["gross_pnl_pct"],
        "gross_pnl_sol": cost_info["gross_pnl_sol"],
        "net_pnl_pct": cost_info["net_pnl_pct"],
        "net_pnl_sol": cost_info["net_pnl_sol"],
        "fees_sol": cost_info["fees_paid_sol"],
        "slippage_through_stop": slippage_through_stop,
        "hold_minutes": round(hold_mins, 1),
        "mcap": state["entry_mcaps"].get(address, 0.0),
        "market_cap": state["entry_mcaps"].get(address, 0.0),
        "liquidity": state["entry_liquidities"].get(address, 0.0),
        "is_reentry": state["is_reentry"].get(address, False),
        "timestamp": datetime.now().isoformat()
    }
    state["closed_trades"].append(trade_record)
    state["total_realized_pnl_sol"] += cost_info["net_pnl_sol"]

    logger.info(
        f"TRADE SETTLED: {symbol} ({sold_size:.3f} SOL) | "
        f"Gross: {cost_info['gross_pnl_sol']:+.4f} SOL ({cost_info['gross_pnl_pct']*100:+.2f}%) | "
        f"Net: {cost_info['net_pnl_sol']:+.4f} SOL ({cost_info['net_pnl_pct']*100:+.2f}%) | "
        f"Fees: {cost_info['fees_paid_sol']:.4f} SOL | StopSlip: {slippage_through_stop*100:.2f}% | Reason: {reason}"
    )

    # Phase 3: Blacklist persistence for losing trades / rugs
    if cost_info["net_pnl_sol"] < 0:
        try:
            from kida_bot.core.safety_checks import record_loss_and_blacklist
            record_loss_and_blacklist(
                mint=address,
                deployer=state.get("entry_deployers", {}).get(address),
                bundler=state.get("entry_bundlers", {}).get(address),
                loss_sol=abs(cost_info["net_pnl_sol"]),
                reason=f"Closed at net loss {cost_info['net_pnl_pct']*100:+.2f}%: {reason}"
            )
        except Exception as e:
            logger.warning(f"Error recording loss to blacklist: {e}")

    # Phase 1: SQLite trade logging
    try:
        from kida_bot.core.db import log_trade
        log_trade({
            "trade_id": f"trade_{staged.stage_id}_{symbol}",
            "mint": address,
            "symbol": symbol,
            "size": sold_size,
            "entry_ts": entry_t,
            "exit_ts": time.time(),
            "entry_price": entry_price,
            "exit_price": effective_exit_price,
            "exit_reason": reason,
            "realized_pnl": cost_info["net_pnl_sol"],
            "realized_pnl_pct": cost_info["net_pnl_pct"],
            "fees_paid": cost_info["fees_paid_sol"],
            "priority_fee": 0.002,
            "quoted_slippage": 0.01,
            "realized_slippage": slippage_through_stop,
            "slippage_through_stop": slippage_through_stop,
            "order_latency_ms": round((time.time() - getattr(staged, "created_at", time.time())) * 1000.0, 1)
        })
    except Exception as e:
        logger.warning(f"Error logging trade to SQLite: {e}")

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
        th["trades_count"] = th.get("trades_count", 0) + 1
        th["cumulative_pnl_sol"] = th.get("cumulative_pnl_sol", 0.0) + pnl_sol
        th["last_pnl_pct"] = pnl_pct
        th["last_exit_time"] = time.time()
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

        from kida_bot.core.volatility import default_volatility_tracker
        default_volatility_tracker.clear_mint(address)

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

            # Cost Model: Dynamic Floors & Break-even derivation
            from kida_bot.core.cost_model import compute_dynamic_floors, estimate_round_trip_cost, calculate_net_pnl
            from kida_bot.core.volatility import default_volatility_tracker, calculate_slippage_through_stop

            # Phase 4: Volatility tracking and Feed Latency Monitoring
            default_volatility_tracker.add_price_sample(addr, cur_price, ts=now)
            lat_ms = float(pos.get("latency_ms") or pos.get("order_latency_ms") or 0.0)
            if lat_ms > 0:
                default_volatility_tracker.record_feed_latency(addr, lat_ms)
            is_blind, feed_lat = default_volatility_tracker.is_feed_blind(addr)
            if is_blind:
                logger.warning(f"BLIND FEED ALERT: {sym} price latency {feed_lat:.0f}ms > threshold! Widening risk boundaries.")

            cost_profile = estimate_round_trip_cost(addr, size_sol, pool_liquidity_usd=pos.get("liquidity", 45000.0))
            vol_stops = default_volatility_tracker.compute_volatility_stops(
                addr, cur_price, round_trip_cost_pct=cost_profile["round_trip_cost_pct"]
            )
            dynamic_floors = compute_dynamic_floors(cost_profile["round_trip_cost_pct"])
            dynamic_tier1_floor = max(TIER1_LOCK_PCT, dynamic_floors["tier1_floor_pct"])
            dynamic_green_vault_trigger = dynamic_floors["green_vault_trigger_pct"]
            dynamic_green_vault_floor = round(cost_profile["round_trip_cost_pct"] + 0.002, 4)
            net_metrics = calculate_net_pnl(size_sol, pnl, fees_paid_sol=cost_profile["total_cost_sol"])

            # Tier 1: Dynamic trigger / floor (at least cost + margin)
            if pnl >= TIER1_TRIGGER_PCT:
                cur_floor = state["active_stop_floors"].get(addr, STOP_LOSS_PCT)
                if dynamic_tier1_floor > cur_floor:
                    state["active_stop_floors"][addr] = dynamic_tier1_floor
                    logger.info(f"BREAK-EVEN LOCKED: {sym} reached +{pnl*100:.1f}%. Dynamic floor raised to +{dynamic_tier1_floor*100:.1f}% (net: +{net_metrics['net_pnl_pct']*100:.1f}%).")

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

        # 4. Volatility-Aware Stop Loss & Ratchet Floor Enforcement (Phase 4)
        active_floor = state["active_stop_floors"].get(addr)
        if active_floor is not None and active_floor > 0:
            if pnl <= active_floor:
                stop_target = entry_price * (1.0 + active_floor)
                stop_slip = calculate_slippage_through_stop(stop_target, cur_price)
                execute_sell(
                    addr, sym,
                    f"RATCHET_FLOOR_EXIT (+{active_floor*100:.1f}% floor hit, PnL: {pnl*100:.1f}%, Slip: {stop_slip*100:.2f}%)",
                    pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol,
                    slippage_through_stop=stop_slip
                )
                continue
        else:
            is_reentry = state["is_reentry"].get(addr, False)
            active_stop = REENTRY_STOP_LOSS_PCT if is_reentry else vol_stops["stop_loss_pct"]
            if pnl <= active_stop:
                stop_target = entry_price * (1.0 + active_stop)
                stop_slip = calculate_slippage_through_stop(stop_target, cur_price)
                stop_label = f"REENTRY_STOP ({REENTRY_STOP_LOSS_PCT*100:.1f}%)" if is_reentry else f"VOLATILITY_STOP ({active_stop*100:.1f}%)"
                execute_sell(
                    addr, sym,
                    f"{stop_label} (PnL: {pnl*100:.1f}%, Slip: {stop_slip*100:.2f}%)",
                    pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol,
                    slippage_through_stop=stop_slip
                )
                continue

        # 4b. Volatility-Aware Rapid Momentum Failure Cut (Phase 4)
        if elapsed_seconds >= RAPID_CUT_WINDOW_SECONDS and pnl <= vol_stops["rapid_cut_pnl"]:
            stop_target = entry_price * (1.0 + vol_stops["rapid_cut_pnl"])
            stop_slip = calculate_slippage_through_stop(stop_target, cur_price)
            execute_sell(
                addr, sym,
                f"VOLATILITY_RAPID_CUT ({elapsed_seconds:.0f}s, PnL: {pnl*100:+.1f}% <= {vol_stops['rapid_cut_pnl']*100:+.1f}%, Slip: {stop_slip*100:.2f}%)",
                pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol,
                slippage_through_stop=stop_slip
            )
            continue

        # 4c. Volatility-Aware Early Bleeder Defense (Phase 4)
        if elapsed_seconds >= BLEEDER_WINDOW_SECONDS and pnl <= vol_stops["bleeder_cut_pnl"]:
            stop_target = entry_price * (1.0 + vol_stops["bleeder_cut_pnl"])
            stop_slip = calculate_slippage_through_stop(stop_target, cur_price)
            execute_sell(
                addr, sym,
                f"VOLATILITY_BLEEDER_CUT ({elapsed_seconds/60:.1f}m, PnL: {pnl*100:+.1f}% <= {vol_stops['bleeder_cut_pnl']*100:+.1f}%, Slip: {stop_slip*100:.2f}%)",
                pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol,
                slippage_through_stop=stop_slip
            )
            continue

        # 5. Dynamic Green Vault / Stagnation Exit: If held >= STAGNATION_TIMEOUT_SECONDS
        if elapsed_seconds >= STAGNATION_TIMEOUT_SECONDS:
            if pnl >= dynamic_green_vault_trigger:
                # Green Vault Profit Protection: Bank 50% profit, lock floor at dynamic_green_vault_floor, and extend hold
                if addr not in state["partial_tp_taken"]:
                    state["active_stop_floors"][addr] = dynamic_green_vault_floor
                    execute_sell(
                        addr, sym,
                        f"GREEN_VAULT_PROFIT_PROTECTION ({elapsed_seconds/60:.1f}m, Gross: {pnl*100:+.1f}% | Net: {net_metrics['net_pnl_pct']*100:+.1f}% >= +{dynamic_green_vault_trigger*100:.1f}% trigger: banking 50%, +{dynamic_green_vault_floor*100:.1f}% floor locked)",
                        pnl, cur_price, entry_price, percent=50, pos_size_sol=size_sol
                    )
                    state["entry_timestamps"][addr] = now  # Reset hold timer for remaining portion
                    continue
                else:
                    # Extended stagnation after partial profit: close remaining 50% to free capital
                    execute_sell(
                        addr, sym,
                        f"STAGNATION_RECYCLE_REMAINDER ({elapsed_seconds/60:.1f}m, Gross: {pnl*100:+.1f}% | Net: {net_metrics['net_pnl_pct']*100:+.1f}% — remaining 50% banked, slot freed)",
                        pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol
                    )
                    continue
            elif pnl < STAGNATION_MIN_PNL_TARGET:
                execute_sell(
                    addr, sym,
                    f"STAGNATION_RECYCLE ({elapsed_seconds/60:.1f}m, Gross: {pnl*100:+.1f}% | Net: {net_metrics['net_pnl_pct']*100:+.1f}% < +{STAGNATION_MIN_PNL_TARGET*100:.1f}% target — slot freed)",
                    pnl, cur_price, entry_price, percent=100, pos_size_sol=size_sol
                )
                continue

# ==============================================================================
# SECTION 8: CYCLE ORCHESTRATION & SUMMARY
# ==============================================================================

def cycle():
    state["cycle_count"] += 1
    now = time.time()
    elapsed_total = now - state["session_start_time"]

    # 0. Emergency Flatten Signal: If FLATTEN file exists in project root, liquidate all open positions
    flatten_file = HERE / "FLATTEN"
    if flatten_file.exists():
        logger.critical("EMERGENCY FLATTEN SIGNAL ENGAGED: Liquidating all open positions immediately...")
        positions = get_positions()
        for p in positions:
            try:
                execute_sell(
                    p["address"], p.get("symbol", "TOKEN"),
                    "EMERGENCY_FLATTEN_TRIGGERED",
                    p.get("pnl", 0.0), p.get("cur_price", 0.0), p.get("entry_price", 0.0),
                    percent=100, pos_size_sol=p.get("size_sol", DEFAULT_TRADE_SIZE_SOL)
                )
            except Exception as e:
                logger.error(f"Failed to flatten {p.get('symbol')}: {e}")
        try:
            flatten_file.unlink()
        except Exception:
            pass
        return

    # 1. Watchdog Heartbeat Emission
    positions = get_positions()
    current_count = len(positions)
    from kida_bot.core.watchdog import write_heartbeat
    write_heartbeat(
        cycle_count=state["cycle_count"],
        active_positions_count=current_count,
        mode="SHADOW" if PAPER_MODE else "LIVE",
        details={"realized_pnl_sol": state["total_realized_pnl_sol"], "slots_used": current_count}
    )

    # 2. Backend Liveness & Watchdog Verification
    status = get_status()
    if not status:
        state["consecutive_backend_failures"] = state.get("consecutive_backend_failures", 0) + 1
        elapsed_fail = state["consecutive_backend_failures"] * FAST_MONITOR_INTERVAL_SECONDS
        logger.warning(f"Backend service unavailable (missed {state['consecutive_backend_failures']} cycles, {elapsed_fail:.1f}s). Retrying...")
        if elapsed_fail >= WATCHDOG_STALL_TIMEOUT_SECONDS:
            logger.critical(f"WATCHDOG ALARM: Backend service down for {elapsed_fail:.1f}s >= threshold {WATCHDOG_STALL_TIMEOUT_SECONDS}s.")
            try:
                send_webhook_alert(f"CRITICAL WATCHDOG ALARM: Backend down for {elapsed_fail:.1f}s. Trading halted.")
            except Exception:
                pass
        return
    else:
        state["consecutive_backend_failures"] = 0

    # 3. Position Risk Management (Always runs when holdings exist)
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

    # 4. Kill Switch Safety Assertion: Stop new entries if daily loss, drawdown, consecutive losses, or HALT tripped
    from kida_bot.core.killswitch import check_killswitch
    ks_tripped, ks_reason = check_killswitch(bot_state=state, portfolio_context=get_portfolio_context())
    if ks_tripped:
        logger.warning(f"KILLSWITCH ACTIVE: {ks_reason}. New entries blocked; managing open positions.")
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
                            regime=regime,
                            price=c.get("price", 0.0)
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
            from kida_bot.strategies.backprop import load_weights, evaluate_candidate_with_weights
            weights = load_weights()
            dynamic_score = evaluate_candidate_with_weights(c, weights)
            council_score = dynamic_score # override mock score
            
            risk_label = c.get("risk_label") or (
                "LOW_RISK" if council_score >= 80 else
                ("MODERATE_RISK" if council_score >= 60 else
                ("ELEVATED_RISK" if council_score >= 40 else "HIGH_RISK"))
            )
            gates_passed = sum(1 for d in council_eval.get("debates", []) if d.get("verdict") == "PASS")
            
            # Save candidate features snapshot for backprop later
            if addr not in state["token_history"]:
                state["token_history"][addr] = {}
            state["token_history"][addr]["entry_candidate_snapshot"] = c

            # Council Veto: block score < 60, fewer than 3 sentinels passing, or explicitly rejected.
            if council_score < 60 or gates_passed < 3 or council_eval.get("verdict") == "REJECTED":
                logger.info(f"COUNCIL VETO ({sym}): Score {council_score}/100 [{risk_label}] ({gates_passed}/5 sentinels) — {council_eval.get('verdict_rationale')}")
                state["cooldown_until"][addr] = now + 900
                continue

            logger.info(f"COUNCIL APPROVED ({sym}): Score {council_score}/100 [{risk_label}] ({gates_passed}/5 sentinels) — {council_eval.get('verdict_rationale')}")

            success = execute_buy(
                addr, sym, mcap, sm_count, liq,
                is_reentry=False,
                council_score=council_score,
                risk_label=risk_label,
                regime=regime,
                price=c.get("price", 0.0)
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
            if SESSION_DURATION_SECONDS > 0 and elapsed >= SESSION_DURATION_SECONDS:
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
