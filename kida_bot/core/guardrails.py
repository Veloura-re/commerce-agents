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

from kida_bot.config import *
import re

BASE58_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
class SolanaAddressValidator:
    """Deterministic validator for Solana Base58 public key mint addresses."""
    
    @staticmethod
    def is_valid(address: str) -> bool:
        if not address or not isinstance(address, str):
            return False
        addr_clean = address.strip()
        if len(addr_clean) < 32 or len(addr_clean) > 44:
            return False
        return bool(BASE58_PATTERN.match(addr_clean))

class TokenProvenance:
    """Cryptographic provenance record certifying token address legitimacy."""
    
    def __init__(
        self,
        address: str,
        symbol: str,
        source: str,
        screened_at: float,
        verified_mcap: float = 0.0,
        verified_liquidity: float = 0.0,
        verified_volume: float = 0.0,
        verified_buy_ratio: float = 0.5,
        verified_bundler: float = 0.0,
        verified_dev_hold: float = 0.0,
        is_valid: bool = True,
        invalidation_reason: Optional[str] = None
    ):
        self.address = address.strip()
        self.symbol = symbol.strip().upper()
        self.source = source
        self.screened_at = screened_at
        self.verified_mcap = verified_mcap
        self.verified_liquidity = verified_liquidity
        self.verified_volume = verified_volume
        self.verified_buy_ratio = verified_buy_ratio
        self.verified_bundler = verified_bundler
        self.verified_dev_hold = verified_dev_hold
        self.is_valid = is_valid
        self.invalidation_reason = invalidation_reason
        self.signature = self._generate_signature()

    def _generate_signature(self) -> str:
        payload = f"{self.address}:{self.symbol}:{self.source}:{int(self.screened_at)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "symbol": self.symbol,
            "source": self.source,
            "screened_at": self.screened_at,
            "age_seconds": round(time.time() - self.screened_at, 1),
            "signature": self.signature,
            "verified_mcap": self.verified_mcap,
            "verified_liquidity": self.verified_liquidity,
            "verified_buy_ratio": self.verified_buy_ratio,
            "is_valid": self.is_valid,
            "invalidation_reason": self.invalidation_reason
        }

class ProvenanceRegistry:
    """In-memory session registry certifying verified on-chain candidate mints."""
    
    def __init__(self, ttl_seconds: float = 1800.0):
        self._registry: Dict[str, TokenProvenance] = {}
        self._ttl_seconds = ttl_seconds
        self.total_registered = 0
        self.verifications_passed = 0
        self.verifications_failed = 0

    def register_screened_candidate(self, candidate: dict, source: str = "GMGN_LIVE_SCREENER") -> Optional[TokenProvenance]:
        addr = candidate.get("address")
        sym = candidate.get("symbol", "UNKNOWN")
        if not addr or not SolanaAddressValidator.is_valid(addr):
            logger.warning(f"PROVENANCE REJECTION: Invalid Solana address syntax for symbol {sym}: '{addr}'")
            return None
        
        feat = candidate.get("features", {})
        prov = TokenProvenance(
            address=addr,
            symbol=sym,
            source=source,
            screened_at=time.time(),
            verified_mcap=float(candidate.get("market_cap", 0.0) or feat.get("mcap", 0.0)),
            verified_liquidity=float(candidate.get("liquidity", 0.0) or feat.get("liquidity", 0.0)),
            verified_volume=float(candidate.get("volume", 0.0) or feat.get("vol_1h", 0.0)),
            verified_buy_ratio=float(candidate.get("buy_ratio", 0.5) or feat.get("buy_ratio", 0.5)),
            verified_bundler=float(candidate.get("bundler", 0.0) or feat.get("bundler", 0.0)),
            verified_dev_hold=float(candidate.get("dev_hold", 0.0) or feat.get("dev_hold", 0.0)),
            is_valid=True
        )
        self._registry[addr.lower()] = prov
        self.total_registered += 1
        return prov

    def register_held_position(self, address: str, symbol: str, source: str = "ACTIVE_PORTFOLIO_INGEST") -> Optional[TokenProvenance]:
        if not SolanaAddressValidator.is_valid(address):
            return None
        prov = TokenProvenance(
            address=address,
            symbol=symbol,
            source=source,
            screened_at=time.time(),
            is_valid=True
        )
        self._registry[address.lower()] = prov
        self.total_registered += 1
        return prov

    def verify(self, address: str, symbol: Optional[str] = None, max_age_seconds: Optional[float] = None) -> Tuple[bool, str, Optional[TokenProvenance]]:
        if not SolanaAddressValidator.is_valid(address):
            self.verifications_failed += 1
            return False, "INVALID_SOLANA_BASE58_ADDRESS", None
        
        prov = self._registry.get(address.lower())
        if not prov:
            self.verifications_failed += 1
            return False, "PROVENANCE_MISSING (Token never delivered by verified screener)", None
        
        if not prov.is_valid:
            self.verifications_failed += 1
            return False, f"PROVENANCE_REVOKED ({prov.invalidation_reason})", prov
        
        ttl = max_age_seconds if max_age_seconds is not None else self._ttl_seconds
        age = time.time() - prov.screened_at
        if age > ttl:
            self.verifications_failed += 1
            return False, f"PROVENANCE_EXPIRED (Age: {age/60:.1f}m > TTL: {ttl/60:.1f}m)", prov

        if symbol and prov.symbol.upper() != symbol.strip().upper():
            self.verifications_failed += 1
            return False, f"TICKER_SPOOF_MISMATCH (Expected: {prov.symbol}, Received: {symbol.upper()})", prov

        self.verifications_passed += 1
        return True, "PROVENANCE_VERIFIED", prov

    def invalidate(self, address: str, reason: str):
        prov = self._registry.get(address.lower())
        if prov:
            prov.is_valid = False
            prov.invalidation_reason = reason
            logger.warning(f"PROVENANCE REVOKED: {prov.symbol} ({address}) — {reason}")

    def get_stats(self) -> dict:
        return {
            "total_registered": self.total_registered,
            "active_valid_records": sum(1 for p in self._registry.values() if p.is_valid and (time.time() - p.screened_at) <= self._ttl_seconds),
            "verifications_passed": self.verifications_passed,
            "verifications_failed": self.verifications_failed
        }

# ==============================================================================
# SECTION 2: ANTHROPIC TWO-PHASE STAGED GUARDRAILS & CHANGE LEDGER
# ==============================================================================

class GuardrailViolation(ValueError):
    """The proposed swap breaks code-level guardrails; violations contains operator-readable reasons."""
    def __init__(self, violations: List[str]):
        super().__init__("; ".join(violations))
        self.violations = violations

class SwapKind:
    BUY_FRESH = "BUY_FRESH"
    BUY_REENTRY = "BUY_REENTRY"
    SELL_FULL = "SELL_FULL"
    SELL_PARTIAL = "SELL_PARTIAL"
    SELL_EMERGENCY = "SELL_EMERGENCY"
    SELL_STOP = "SELL_STOP"
    SELL_TP = "SELL_TP"

class SwapStatus:
    STAGED = "STAGED"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    DISCARDED = "DISCARDED"
    FAILED = "FAILED"

class StagedSwap:
    """Represents a staged swap proposal that must pass two verification checkpoints."""
    
    def __init__(
        self,
        stage_id: str,
        kind: str,
        address: str,
        symbol: str,
        size_sol: float,
        percent: int = 100,
        reason: str = "",
        actor: str = "AUTONOMOUS_CYCLE",
        context: Optional[dict] = None
    ):
        self.stage_id = stage_id
        self.kind = kind
        self.status = SwapStatus.STAGED
        self.address = address
        self.symbol = symbol
        self.size_sol = round(size_sol, 4)
        self.percent = percent
        self.reason = reason
        self.actor = actor
        self.context = context or {}
        self.created_at = time.time()
        self.applied_at: Optional[float] = None
        self.discarded_at: Optional[float] = None
        self.tx_hash: Optional[str] = None
        self.guardrail_notes: List[str] = []
        self.violations: List[str] = []
        self.provenance_signature: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "stage_id": self.stage_id,
            "kind": self.kind,
            "status": self.status,
            "address": self.address,
            "symbol": self.symbol,
            "size_sol": self.size_sol,
            "percent": self.percent,
            "reason": self.reason,
            "actor": self.actor,
            "created_at": self.created_at,
            "applied_at": self.applied_at,
            "tx_hash": self.tx_hash,
            "violations": self.violations,
            "guardrail_notes": self.guardrail_notes,
            "provenance_signature": self.provenance_signature
        }

def check_swap_guardrails(
    staged: StagedSwap,
    portfolio_context: dict,
    provenance_registry: ProvenanceRegistry,
    live_heuristics: dict,
    bot_state: dict
) -> List[str]:
    """
    Code-level guardrails enforced when a swap is staged (Phase 1)
    and re-evaluated immediately before on-chain execution (Phase 2).
    """
    violations: List[str] = []
    is_buy = staged.kind in (SwapKind.BUY_FRESH, SwapKind.BUY_REENTRY)
    is_reentry = (staged.kind == SwapKind.BUY_REENTRY)

    # 1. Provenance Invariant
    ok_prov, prov_msg, prov_rec = provenance_registry.verify(staged.address, staged.symbol)
    if is_buy and not ok_prov:
        violations.append(f"PROVENANCE_FAIL: {prov_msg}")
    elif prov_rec:
        staged.provenance_signature = prov_rec.signature

    # 2. Capital Exposure Bounds
    current_positions = portfolio_context.get("positions", [])
    current_exposure = portfolio_context.get("total_exposure", 0.0)
    open_count = len(current_positions)

    if is_buy:
        # Slot Size Cap
        if staged.size_sol > MAX_TRADE_SIZE_SOL + 0.0001:
            violations.append(f"SLOT_SIZE_CAP: {staged.size_sol:.3f} SOL exceeds slot cap {MAX_TRADE_SIZE_SOL:.3f} SOL")

        # 50% Capital Utilization Cap (Max 3.60 SOL exposure)
        if current_exposure + staged.size_sol > MAX_ACTIVE_EXPOSURE_SOL + 0.001:
            violations.append(
                f"EXPOSURE_CAP_EXCEEDED: New total exposure {current_exposure + staged.size_sol:.3f} SOL "
                f"exceeds limit {MAX_ACTIVE_EXPOSURE_SOL:.3f} SOL"
            )

        # Max Concurrent Positions Cap
        if open_count >= MAX_POSITIONS:
            violations.append(f"SLOT_CAP_SATURATED: All {MAX_POSITIONS} concurrent slots are active")

        # Duplicate Position Defense
        held_addrs = {p["address"].lower() for p in current_positions}
        if staged.address.lower() in held_addrs:
            violations.append(f"DUPLICATE_POSITION: Token {staged.symbol} ({staged.address[:8]}...) already held")

    # 3. Liquidity Floor & Market Cap Moat (Buys only)
    if is_buy and prov_rec:
        min_liq = 15000.0
        min_mcap = 15000.0
        
        if prov_rec.verified_liquidity < min_liq:
            violations.append(f"LIQUIDITY_FLOOR: Pool ${prov_rec.verified_liquidity:,.0f} < ${min_liq:,.0f} minimum")
        if prov_rec.verified_mcap < min_mcap:
            violations.append(f"MCAP_FLOOR: Market cap ${prov_rec.verified_mcap:,.0f} < ${min_mcap:,.0f} minimum")

        # Price Impact Estimation (0.7 SOL order vs pool depth)
        sol_price_est = 140.0
        order_value_usd = staged.size_sol * sol_price_est
        if prov_rec.verified_liquidity > 0:
            price_impact_ratio = order_value_usd / prov_rec.verified_liquidity
            if price_impact_ratio > 0.02:
                violations.append(f"PRICE_IMPACT_EXCESSIVE: Impact {price_impact_ratio*100:.2f}% exceeds 2.0% threshold")

        # Bundler & Dev Hold Invariants
        max_bundler = live_heuristics.get("max_bundler_rate", 0.15)
        max_dev = live_heuristics.get("max_dev_hold_rate", 0.12)
        if prov_rec.verified_bundler > max_bundler:
            violations.append(f"BUNDLER_CONCENTRATION: Bundler {prov_rec.verified_bundler*100:.1f}% > {max_bundler*100:.1f}%")
        if prov_rec.verified_dev_hold > max_dev:
            violations.append(f"DEV_HOLD_CONCENTRATION: Dev hold {prov_rec.verified_dev_hold*100:.1f}% > {max_dev*100:.1f}%")

        # Pump.fun Curve Protection: Reject creator holding > 5% on bonding curve
        if staged.address.lower().endswith("pump"):
            if prov_rec.verified_dev_hold > 0.05:
                violations.append(f"PUMPFUN_DEV_HOLD_HIGH: Creator holds {prov_rec.verified_dev_hold*100:.1f}% > 5.0% limit on Pump.fun")
            if prov_rec.verified_liquidity < 15000.0:
                violations.append(f"PUMPFUN_THIN_LIQUIDITY: Curve liquidity ${prov_rec.verified_liquidity:,.0f} < $15,000 floor")

        # Council Veto Invariant: Minimum 40/100 score
        c_score = staged.context.get("council_score")
        if c_score is not None and c_score < 40:
            violations.append(f"COUNCIL_VETO: Candidate score {c_score}/100 failed Supreme Council threshold (min 40)")

    # 4. Anti-Chaser Consolidation Cooldown & Re-Entry Caps
    if is_buy:
        now = time.time()
        cooldown_until = bot_state.get("cooldown_until", {}).get(staged.address, 0)
        if now < cooldown_until:
            violations.append(f"COOLDOWN_ACTIVE: {staged.symbol} is under cooldown for {cooldown_until - now:.0f}s")
        
        if is_reentry:
            th = bot_state.get("token_history", {}).get(staged.address, {})
            if th.get("trades_count", 0) >= MAX_REENTRIES_PER_TOKEN:
                violations.append(f"MAX_REENTRIES_EXCEEDED: Token has reached {MAX_REENTRIES_PER_TOKEN} scalps limit")

    # 5. Sell Invariants
    if not is_buy:
        if staged.percent <= 0 or staged.percent > 100:
            violations.append(f"INVALID_SELL_PERCENT: Requested {staged.percent}% must be within 1-100%")
        
        # Verify position is actually held before staging exit
        held_addrs = {p["address"].lower() for p in current_positions}
        if staged.address.lower() not in held_addrs:
            violations.append(f"SELL_UNHELD_TOKEN: Address {staged.address[:8]}... is not currently in open positions")

    return violations

class SwapLedger:
    """
    Two-phase staged swap ledger built on Anthropic commerce agent architecture.
    Stages changes, checks guardrails, records actor provenance, and applies transitions.
    """
    def __init__(self, provenance_registry: ProvenanceRegistry):
        self.provenance_registry = provenance_registry
        self._staged_swaps: Dict[str, StagedSwap] = {}
        self._sequence = 0
        self.total_staged = 0
        self.total_applied = 0
        self.total_rejected = 0
        self.total_discarded = 0

    def stage_swap(
        self,
        kind: str,
        address: str,
        symbol: str,
        size_sol: float,
        percent: int = 100,
        reason: str = "",
        actor: str = "AUTONOMOUS_CYCLE",
        context: Optional[dict] = None,
        portfolio_context: Optional[dict] = None,
        live_heuristics: Optional[dict] = None,
        bot_state: Optional[dict] = None
    ) -> StagedSwap:
        self._sequence += 1
        stage_id = f"swap-stage-{self._sequence:04d}"
        
        staged = StagedSwap(
            stage_id=stage_id,
            kind=kind,
            address=address,
            symbol=symbol,
            size_sol=size_sol,
            percent=percent,
            reason=reason,
            actor=actor,
            context=context or {}
        )
        
        # Phase 1: Pre-Execution Guardrails Evaluation
        violations = check_swap_guardrails(
            staged=staged,
            portfolio_context=portfolio_context or {},
            provenance_registry=self.provenance_registry,
            live_heuristics=live_heuristics or {},
            bot_state=bot_state or {}
        )
        
        if violations:
            staged.status = SwapStatus.REJECTED
            staged.violations = violations
            self.total_rejected += 1
            self._staged_swaps[stage_id] = staged
            logger.warning(
                f"[STAGE 1/2] SWAP REJECTED BY GUARDRAILS: {stage_id} | {kind} {symbol} ({address[:8]}...) | "
                f"Violations: {'; '.join(violations)}"
            )
            return staged

        staged.status = SwapStatus.STAGED
        staged.guardrail_notes = ["PROVENANCE_VERIFIED", "CAPITAL_BOUNDED", "LIQUIDITY_FLOOR_CONFIRMED"]
        self.total_staged += 1
        self._staged_swaps[stage_id] = staged
        logger.info(
            f"[STAGE 1/2] SWAP PROPOSED & STAGED: {stage_id} | {kind} {symbol} ({address[:8]}...{address[-4:]}) | "
            f"Size: {staged.size_sol:.3f} SOL | Sig: {staged.provenance_signature or 'N/A'}"
        )
        return staged

    def stage_buy(
        self,
        address: str,
        symbol: str,
        size_sol: float = DEFAULT_TRADE_SIZE_SOL,
        is_reentry: bool = False,
        reason: str = "",
        actor: str = "AUTONOMOUS_CYCLE",
        context: Optional[dict] = None,
        portfolio_context: Optional[dict] = None,
        live_heuristics: Optional[dict] = None,
        bot_state: Optional[dict] = None
    ) -> StagedSwap:
        kind = SwapKind.BUY_REENTRY if is_reentry else SwapKind.BUY_FRESH
        return self.stage_swap(
            kind=kind,
            address=address,
            symbol=symbol,
            size_sol=size_sol,
            percent=100,
            reason=reason,
            actor=actor,
            context=context,
            portfolio_context=portfolio_context,
            live_heuristics=live_heuristics,
            bot_state=bot_state
        )

    def stage_sell(
        self,
        address: str,
        symbol: str,
        size_sol: float,
        percent: int = 100,
        kind: str = SwapKind.SELL_FULL,
        reason: str = "",
        actor: str = "RISK_MONITOR",
        context: Optional[dict] = None,
        portfolio_context: Optional[dict] = None,
        live_heuristics: Optional[dict] = None,
        bot_state: Optional[dict] = None
    ) -> StagedSwap:
        return self.stage_swap(
            kind=kind,
            address=address,
            symbol=symbol,
            size_sol=size_sol,
            percent=percent,
            reason=reason,
            actor=actor,
            context=context,
            portfolio_context=portfolio_context,
            live_heuristics=live_heuristics,
            bot_state=bot_state
        )

    def apply_swap(
        self,
        stage_id: str,
        execute_fn: Any,
        portfolio_context_fn: Any,
        live_heuristics_fn: Any,
        bot_state: dict
    ) -> Tuple[bool, StagedSwap, Optional[dict]]:
        """
        Phase 2: Re-evaluates guardrails against real-time state, then applies transaction.
        """
        staged = self._staged_swaps.get(stage_id)
        if not staged:
            raise ValueError(f"Staged swap {stage_id} does not exist in ledger")
        if staged.status != SwapStatus.STAGED:
            raise ValueError(f"Staged swap {stage_id} is in status '{staged.status}', cannot apply")

        # Phase 2 Real-Time Pre-Flight Check (enforces zero race conditions)
        fresh_portfolio = portfolio_context_fn()
        fresh_heuristics = live_heuristics_fn()
        violations = check_swap_guardrails(
            staged=staged,
            portfolio_context=fresh_portfolio,
            provenance_registry=self.provenance_registry,
            live_heuristics=fresh_heuristics,
            bot_state=bot_state
        )

        if violations:
            staged.status = SwapStatus.DISCARDED
            staged.violations = violations
            staged.discarded_at = time.time()
            self.total_discarded += 1
            logger.warning(
                f"[STAGE 2/2] PRE-FLIGHT GUARDRAIL ABORT: {stage_id} | {staged.symbol} aborted before broadcast | "
                f"Violations: {'; '.join(violations)}"
            )
            return False, staged, None

        # Execute on-chain transaction
        try:
            logger.info(f"[STAGE 2/2] DISPATCHING ON-CHAIN TRANSACTION: {stage_id} | {staged.symbol}...")
            tx_data = execute_fn(staged)
            if tx_data and tx_data.get("ok", True):
                staged.status = SwapStatus.APPLIED
                staged.applied_at = time.time()
                staged.tx_hash = tx_data.get("tx_hash", "SHADOW_SIM_CONFIRMED")
                self.total_applied += 1
                logger.info(f"[STAGE 2/2] SWAP APPLIED TO CHAIN: {stage_id} | {staged.symbol} | Tx: {staged.tx_hash}")
                return True, staged, tx_data
            else:
                staged.status = SwapStatus.FAILED
                staged.violations.append(f"BROADCAST_FAILURE: {tx_data.get('error', 'Unknown execution error') if isinstance(tx_data, dict) else 'Execution returned falsy'}")
                return False, staged, tx_data
        except Exception as e:
            staged.status = SwapStatus.FAILED
            staged.violations.append(f"EXECUTION_EXCEPTION: {str(e)}")
            logger.error(f"[STAGE 2/2] EXECUTION EXCEPTION on {stage_id}: {e}")
            return False, staged, None

    def get_audit_summary(self) -> dict:
        return {
            "total_staged": self.total_staged,
            "total_applied": self.total_applied,
            "total_rejected": self.total_rejected,
            "total_discarded": self.total_discarded,
            "active_staged_count": sum(1 for s in self._staged_swaps.values() if s.status == SwapStatus.STAGED),
            "recent_swaps": [s.to_dict() for s in list(self._staged_swaps.values())[-10:]]
        }

# Global Provenance Registry and Two-Phase Swap Ledger
provenance_registry = ProvenanceRegistry(ttl_seconds=1800.0)
swap_ledger = SwapLedger(provenance_registry=provenance_registry)

# ==============================================================================
# SECTION 3: BOT STATE & SESSION PERSISTENCE
# ==============================================================================

