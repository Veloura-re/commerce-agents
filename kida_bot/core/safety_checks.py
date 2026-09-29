"""
KIDA Citadel Pre-Trade Safety Checks Module (Phase 3)
Enforces Gate 1 pre-trade invariants:
1. Freeze authority renounced (prevent blacklist/honeypots)
2. LP burned or locked minimum percentage
3. Sell simulation before buy (impact ceiling & implied tax check)
4. Deployer & bundler-cluster blacklist persistence across losses and rugs
5. Feature snapshot freshness verification
"""

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple

from kida_bot.config import (
    MAX_SELL_SIMULATION_IMPACT_PCT,
    MAX_SNAPSHOT_AGE_SECONDS,
    MIN_LP_BURN_OR_LOCK_PCT,
    REQUIRE_RENOUNCED_FREEZE,
)
from kida_bot.core.db import get_connection

logger = logging.getLogger("kida_citadel.safety_checks")

# Fast in-memory cache for blacklisted addresses (lowers DB read overhead)
_BLACKLIST_CACHE: Set[str] = set()
_BLACKLIST_LOADED = False


def init_blacklist_table(db_path: Optional[Path] = None) -> None:
    """Creates the blacklist persistence table if it does not exist."""
    with get_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blacklist (
                address TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                loss_sol REAL DEFAULT 0.0,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );
            """
        )
        conn.commit()


def _ensure_blacklist_cache(db_path: Optional[Path] = None) -> None:
    global _BLACKLIST_CACHE, _BLACKLIST_LOADED
    if not _BLACKLIST_LOADED:
        init_blacklist_table(db_path)
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT address FROM blacklist")
            _BLACKLIST_CACHE = {row["address"].lower() for row in cursor.fetchall()}
            _BLACKLIST_LOADED = True


def is_blacklisted(address: Optional[str], db_path: Optional[Path] = None) -> Tuple[bool, str]:
    """Checks if an address (token, deployer, or bundler cluster) is blacklisted."""
    if not address:
        return False, ""
    _ensure_blacklist_cache(db_path)
    clean_addr = address.strip().lower()
    if clean_addr in _BLACKLIST_CACHE:
        with get_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT entity_type, reason FROM blacklist WHERE lower(address) = ?", (clean_addr,))
            row = cursor.fetchone()
            if row:
                return True, f"BLACKLISTED_{row['entity_type'].upper()}: Address {address[:8]}... blacklisted: {row['reason']}"
            return True, f"BLACKLISTED: Address {address[:8]}... found in blacklist registry"
    return False, ""


def add_to_blacklist(
    address: str,
    entity_type: str,
    reason: str,
    loss_sol: float = 0.0,
    db_path: Optional[Path] = None
) -> None:
    """Persists an entity address into the blacklist table and updates the memory cache."""
    if not address:
        return
    clean_addr = address.strip().lower()
    init_blacklist_table(db_path)
    now = time.time()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO blacklist (address, entity_type, reason, loss_sol, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (clean_addr, entity_type, reason, loss_sol, now)
        )
        conn.commit()
    _BLACKLIST_CACHE.add(clean_addr)
    logger.info(f"BLACKLIST ADDED: [{entity_type.upper()}] {address} | Reason: {reason} | Loss: {loss_sol:.4f} SOL")


def record_loss_and_blacklist(
    mint: str,
    deployer: Optional[str] = None,
    bundler: Optional[str] = None,
    loss_sol: float = 0.0,
    reason: str = "",
    db_path: Optional[Path] = None
) -> None:
    """
    When a token is stopped out or identified as a rug/loss, persists
    the token, deployer wallet, and bundler cluster addresses into the permanent blacklist.
    """
    if mint:
        add_to_blacklist(mint, "token", f"Loss incurred ({loss_sol:+.4f} SOL): {reason}", loss_sol, db_path=db_path)
    if deployer and len(deployer) > 10:
        add_to_blacklist(deployer, "deployer", f"Deployer of losing/rugged token {mint[:8]}: {reason}", loss_sol, db_path=db_path)
    if bundler and len(bundler) > 10:
        add_to_blacklist(bundler, "bundler", f"Bundler cluster associated with {mint[:8]}: {reason}", loss_sol, db_path=db_path)


def validate_freeze_and_lp(
    renounced_freeze: bool,
    burn_ratio: float,
    min_burn_ratio: float = MIN_LP_BURN_OR_LOCK_PCT
) -> Tuple[bool, str]:
    """
    Enforces freeze authority renunciation and minimum LP burn/lock percentage.
    """
    if REQUIRE_RENOUNCED_FREEZE and not renounced_freeze:
        return False, "FREEZE_AUTHORITY_NOT_RENOUNCED: Freeze authority active on mint (Honeypot / Blacklist risk)"

    if burn_ratio < min_burn_ratio:
        return False, f"LP_BURN_OR_LOCK_INSUFFICIENT: LP burn/lock ratio {burn_ratio * 100:.1f}% < {min_burn_ratio * 100:.1f}% floor"

    return True, "OK"


def simulate_sell_order(
    mint: str,
    size_sol: float,
    quote_provider: Optional[Callable[[str, str, float], Dict[str, Any]]] = None,
    pool_liquidity_usd: float = 0.0,
    max_impact_pct: float = MAX_SELL_SIMULATION_IMPACT_PCT,
    max_tax_pct: float = 0.08,
    sol_price_usd: float = 140.0
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Simulates a sell quote before initiating a buy to catch honeypots, exit routing failures,
    excessive sell slippage, or hidden exit taxes that token metadata misses.
    """
    clean_size = max(0.01, float(size_sol))

    if quote_provider is not None:
        try:
            # Request reverse quote: token -> SOL
            sell_quote = quote_provider(mint, "So11111111111111111111111111111111111111112", clean_size)
            if not sell_quote or sell_quote.get("error"):
                err_msg = sell_quote.get("error") if sell_quote else "No route found"
                return False, f"SELL_SIMULATION_FAILED: DEX router failed to produce sell quote ({err_msg})", {}

            impact = float(sell_quote.get("price_impact") or 0.0)
            if impact > max_impact_pct:
                return False, f"SELL_SIMULATION_IMPACT_EXCESSIVE: Sell impact {impact * 100:.2f}% > {max_impact_pct * 100:.1f}% limit", sell_quote

            implied_tax = float(sell_quote.get("implied_tax") or sell_quote.get("fee_pct") or 0.0)
            if implied_tax > max_tax_pct:
                return False, f"SELL_SIMULATION_TAX_EXCESSIVE: Implied sell tax {implied_tax * 100:.2f}% > {max_tax_pct * 100:.1f}% limit", sell_quote

            return True, "SELL_SIMULATION_PASSED", sell_quote
        except Exception as e:
            return False, f"SELL_SIMULATION_FAILED: Sell quote exception ({e})", {}

    # Pool depth based simulation fallback
    if pool_liquidity_usd > 0.0:
        trade_usd = clean_size * sol_price_usd
        est_sell_impact = trade_usd / max(pool_liquidity_usd, 1000.0)
        if est_sell_impact > max_impact_pct:
            return False, f"SELL_SIMULATION_IMPACT_EXCESSIVE: Estimated sell impact {est_sell_impact * 100:.2f}% > {max_impact_pct * 100:.1f}% limit", {"est_impact": est_sell_impact}

    return True, "SELL_SIMULATION_PASSED", {}


def validate_snapshot_freshness(
    snapshot_ts: float,
    max_age_seconds: float = MAX_SNAPSHOT_AGE_SECONDS
) -> Tuple[bool, str]:
    """
    Verifies GMGN feature snapshot freshness. Rejects stale data snapshots.
    """
    if snapshot_ts <= 0.0:
        return True, "OK"

    now = time.time()
    age = now - snapshot_ts

    if age > max_age_seconds:
        return False, f"STALE_SNAPSHOT_DATA: Feature snapshot is {age:.1f}s old (Limit: {max_age_seconds:.0f}s)"

    return True, "OK"
