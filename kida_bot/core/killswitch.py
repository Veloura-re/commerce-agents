import os
import pathlib
import logging
from typing import Tuple, Dict, Any, Optional

logger = logging.getLogger("KillSwitchCitadel")

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

def check_killswitch(bot_state: Optional[Dict[str, Any]] = None, portfolio_context: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """
    Evaluates system safety invariants before any trade entry.
    Returns: (is_tripped: bool, reason: str).
    When tripped, new trade entries are prohibited while open position exits continue normally.
    """
    from kida_bot.config import (
        DAILY_LOSS_CAP_SOL,
        MAX_DRAWDOWN_PCT,
        MAX_CONSECUTIVE_LOSSES,
        HALT_FILE_PATH,
        TOTAL_BANKROLL_SOL,
        DEFAULT_TRADE_SIZE_SOL
    )

    # 1. Manual Circuit Breaker: HALT file in project root
    halt_file = HALT_FILE_PATH if isinstance(HALT_FILE_PATH, pathlib.Path) else (PROJECT_ROOT / str(HALT_FILE_PATH))
    if halt_file.exists():
        msg = f"MANUAL_HALT_ENGAGED: Execution halt triggered via file '{halt_file.name}'"
        return True, msg

    if not bot_state:
        return False, "HEALTHY"

    # 2. Daily Realized Loss Cap
    realized_pnl = float(bot_state.get("total_realized_pnl_sol", 0.0))
    if realized_pnl < 0 and abs(realized_pnl) >= DAILY_LOSS_CAP_SOL:
        msg = f"DAILY_LOSS_CAP_TRIPPED: Realized loss {abs(realized_pnl):.4f} SOL exceeds daily ceiling {DAILY_LOSS_CAP_SOL:.4f} SOL"
        return True, msg

    # 3. Consecutive Loss Count
    closed_trades = bot_state.get("closed_trades", [])
    consecutive_losses = 0
    for trade in reversed(closed_trades):
        trade_pnl = trade.get("pnl_sol")
        if trade_pnl is None:
            trade_pnl = trade.get("pnl", 0.0)
        if trade_pnl < 0:
            consecutive_losses += 1
        else:
            break

    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        msg = f"CONSECUTIVE_LOSS_CIRCUIT_BREAKER: {consecutive_losses} consecutive losses hit threshold ({MAX_CONSECUTIVE_LOSSES})"
        return True, msg

    # 4. Max Drawdown from Session Peak Equity
    active_positions = []
    if portfolio_context and isinstance(portfolio_context, dict):
        active_positions = portfolio_context.get("positions", [])
    elif "active_positions" in bot_state:
        active_positions = bot_state.get("active_positions", [])

    unrealized_sol = 0.0
    for p in active_positions:
        p_pnl = p.get("pnl", 0.0)
        p_size = p.get("size_sol", DEFAULT_TRADE_SIZE_SOL)
        unrealized_sol += (p_pnl * p_size)

    current_equity = TOTAL_BANKROLL_SOL + realized_pnl + unrealized_sol
    peak_equity = bot_state.get("peak_session_equity_sol", TOTAL_BANKROLL_SOL)
    if current_equity > peak_equity:
        peak_equity = current_equity
        bot_state["peak_session_equity_sol"] = peak_equity

    if peak_equity > 0:
        drawdown_pct = (peak_equity - current_equity) / peak_equity
        if drawdown_pct >= MAX_DRAWDOWN_PCT:
            msg = (
                f"MAX_DRAWDOWN_TRIPPED: Session drawdown {drawdown_pct*100:.1f}% exceeds limit "
                f"{MAX_DRAWDOWN_PCT*100:.1f}% (Peak: {peak_equity:.4f} SOL, Current: {current_equity:.4f} SOL)"
            )
            return True, msg

    return False, "HEALTHY"


def get_killswitch_status(bot_state: Optional[Dict[str, Any]] = None, portfolio_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Returns structured killswitch diagnostics.
    """
    from kida_bot.config import (
        DAILY_LOSS_CAP_SOL,
        MAX_DRAWDOWN_PCT,
        MAX_CONSECUTIVE_LOSSES,
        HALT_FILE_PATH,
        TOTAL_BANKROLL_SOL,
        DEFAULT_TRADE_SIZE_SOL
    )

    halt_file = HALT_FILE_PATH if isinstance(HALT_FILE_PATH, pathlib.Path) else (PROJECT_ROOT / str(HALT_FILE_PATH))
    halt_present = halt_file.exists()
    
    tripped, reason = check_killswitch(bot_state=bot_state, portfolio_context=portfolio_context)
    
    realized_pnl = float(bot_state.get("total_realized_pnl_sol", 0.0)) if bot_state else 0.0
    closed_trades = bot_state.get("closed_trades", []) if bot_state else []
    
    consecutive_losses = 0
    for trade in reversed(closed_trades):
        trade_pnl = trade.get("pnl_sol")
        if trade_pnl is None:
            trade_pnl = trade.get("pnl", 0.0)
        if trade_pnl < 0:
            consecutive_losses += 1
        else:
            break

    peak_equity = bot_state.get("peak_session_equity_sol", TOTAL_BANKROLL_SOL) if bot_state else TOTAL_BANKROLL_SOL
    
    return {
        "is_tripped": tripped,
        "reason": reason,
        "halt_file_present": halt_present,
        "realized_pnl_sol": realized_pnl,
        "daily_loss_cap_sol": DAILY_LOSS_CAP_SOL,
        "consecutive_losses": consecutive_losses,
        "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
        "peak_session_equity_sol": peak_equity,
        "max_drawdown_limit_pct": MAX_DRAWDOWN_PCT
    }
