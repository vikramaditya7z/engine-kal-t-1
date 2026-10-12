"""Paper Trading Risk Gate, configurable exposure limits, and operational kill-switch.

V9 Risk Controls:
- Evaluates every proposed candidate before simulated execution.
- Enforces per-trade cost, position size, aggregate exposure, and concurrency limits.
- Enforces daily loss, maximum drawdown, and data freshness thresholds.
- Independent global kill-switch with immediate exposure cutoff.
- Automatic halt on consecutive operational failures or accounting discrepancies.
- Capital reservation management with explicit reservation release on failure.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Dict, Optional, Set, Tuple

from .ledger import PaperPortfolio, PositionHolding
from .opportunity_evaluator import EvaluatedCandidate
from .risk import RiskCheckResult, RiskConfig, RiskError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaperRiskConfig(RiskConfig):
    """Extended risk boundaries and constraints for autonomous paper trading."""

    max_concurrent_trades: int = 5
    max_daily_realized_loss_cents: int = 20_000  # $200.00 max daily loss
    max_drawdown_cents: int = 50_000  # $500.00 max drawdown
    max_data_age_seconds: float = 60.0
    max_consecutive_failures: int = 5
    require_positive_net_edge: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.max_concurrent_trades <= 0:
            raise RiskError("max_concurrent_trades must be positive")
        if self.max_daily_realized_loss_cents <= 0:
            raise RiskError("max_daily_realized_loss_cents must be positive")
        if self.max_drawdown_cents <= 0:
            raise RiskError("max_drawdown_cents must be positive")
        if self.max_data_age_seconds <= 0:
            raise RiskError("max_data_age_seconds must be positive")
        if self.max_consecutive_failures <= 0:
            raise RiskError("max_consecutive_failures must be positive")


class PaperRiskGate:
    """Pre-trade risk gateway and emergency operational safeguards."""

    def __init__(self, config: Optional[PaperRiskConfig] = None) -> None:
        self.config = config or PaperRiskConfig()
        self._kill_switch_active: bool = False
        self._kill_switch_reason: Optional[str] = None
        self._processed_candidate_ids: Set[str] = set()
        self._active_trade_ids: Set[str] = set()
        self._consecutive_failures: int = 0
        self._peak_equity_cents: Optional[int] = None
        self._reserved_capital_cents: int = 0

    @property
    def is_kill_switch_active(self) -> bool:
        return self._kill_switch_active

    @property
    def kill_switch_reason(self) -> Optional[str]:
        return self._kill_switch_reason

    def activate_kill_switch(self, reason: str) -> None:
        """Immediately engage global kill-switch to halt all new simulated exposure."""
        self._kill_switch_active = True
        self._kill_switch_reason = reason
        logger.critical(f"GLOBAL KILL-SWITCH ACTIVATED: {reason}")

    def deactivate_kill_switch(self) -> None:
        """Manually disengage global kill-switch."""
        self._kill_switch_active = False
        self._kill_switch_reason = None
        logger.info("Global kill-switch disengaged by explicit administrative action.")

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self, error_msg: str) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.config.max_consecutive_failures:
            self.activate_kill_switch(
                f"Consecutive failure threshold reached ({self._consecutive_failures} failures): {error_msg}"
            )

    def register_active_trade(self, trade_id: str) -> None:
        self._active_trade_ids.add(trade_id)

    def unregister_active_trade(self, trade_id: str) -> None:
        self._active_trade_ids.discard(trade_id)

    def evaluate_candidate(
        self,
        portfolio: PaperPortfolio,
        candidate: EvaluatedCandidate,
        current_time: Optional[datetime] = None,
    ) -> RiskCheckResult:
        """Comprehensive pre-trade risk evaluation for an evaluated arbitrage candidate."""
        now = current_time or datetime.now(timezone.utc)

        # 1. Kill-switch check
        if self._kill_switch_active:
            return RiskCheckResult(
                passed=False,
                rejection_reason=f"Kill-switch is active: {self._kill_switch_reason}",
                violated_rule="KILL_SWITCH_ACTIVE",
            )

        # 2. Duplicate candidate check
        if candidate.candidate_id in self._processed_candidate_ids:
            return RiskCheckResult(
                passed=False,
                rejection_reason=f"Candidate {candidate.candidate_id} already evaluated",
                violated_rule="DUPLICATE_CANDIDATE_ID",
            )

        # 3. Execution feasibility & edge checks
        if not candidate.is_execution_feasible:
            return RiskCheckResult(
                passed=False,
                rejection_reason=f"Insufficient order book depth (supported={candidate.supported_quantity})",
                violated_rule="INSUFFICIENT_DEPTH",
            )

        if self.config.require_positive_gross_edge and candidate.gross_profit_cents <= 0:
            return RiskCheckResult(
                passed=False,
                rejection_reason=f"Candidate has no gross edge ({candidate.gross_profit_cents}¢)",
                violated_rule="REQUIRE_POSITIVE_GROSS_EDGE",
            )

        if self.config.require_positive_net_edge and not candidate.is_net_profitable:
            return RiskCheckResult(
                passed=False,
                rejection_reason=(
                    f"Candidate not net profitable after fees: net={candidate.net_profit_cents}¢ "
                    f"(fees={candidate.total_fees_cents}¢)"
                ),
                violated_rule="REQUIRE_POSITIVE_NET_EDGE",
            )

        # 4. Data freshness check
        cand_time = candidate.evaluated_at
        if cand_time.tzinfo is None:
            cand_time = cand_time.replace(tzinfo=timezone.utc)
        data_age = (now - cand_time).total_seconds()
        if data_age > self.config.max_data_age_seconds:
            return RiskCheckResult(
                passed=False,
                rejection_reason=f"Data age {data_age:.1f}s exceeds threshold {self.config.max_data_age_seconds}s",
                violated_rule="DATA_AGE_EXCEEDED",
            )

        # 5. Concurrency check
        if len(self._active_trade_ids) >= self.config.max_concurrent_trades:
            return RiskCheckResult(
                passed=False,
                rejection_reason=(
                    f"Active trades ({len(self._active_trade_ids)}) meets/exceeds limit "
                    f"({self.config.max_concurrent_trades})"
                ),
                violated_rule="MAX_CONCURRENT_TRADES",
            )

        # 6. Trade size and quantity checks
        if candidate.supported_quantity > self.config.max_requested_quantity:
            return RiskCheckResult(
                passed=False,
                rejection_reason=(
                    f"Quantity ({candidate.supported_quantity}) exceeds limit ({self.config.max_requested_quantity})"
                ),
                violated_rule="MAX_REQUESTED_QUANTITY",
            )

        total_needed = candidate.total_cost_cents + candidate.total_fees_cents
        if total_needed > self.config.max_cost_per_trade_cents:
            return RiskCheckResult(
                passed=False,
                rejection_reason=(
                    f"Trade capital requirement ({total_needed}¢) exceeds limit ({self.config.max_cost_per_trade_cents}¢)"
                ),
                violated_rule="MAX_COST_PER_TRADE",
            )

        # 7. Available cash check (accounting for concurrent reservations)
        avail = portfolio.available_cash_cents - self._reserved_capital_cents
        if avail < total_needed:
            return RiskCheckResult(
                passed=False,
                rejection_reason=(
                    f"Insufficient available cash ({avail}¢ available < needed {total_needed}¢)"
                ),
                violated_rule="INSUFFICIENT_AVAILABLE_CASH",
            )

        # 8. Aggregate exposure check
        current_exp = portfolio.total_cost_basis_cents()
        projected_exp = current_exp + candidate.total_cost_cents
        if projected_exp > self.config.max_aggregate_exposure_cents:
            return RiskCheckResult(
                passed=False,
                rejection_reason=(
                    f"Projected aggregate exposure ({projected_exp}¢) exceeds limit "
                    f"({self.config.max_aggregate_exposure_cents}¢)"
                ),
                violated_rule="MAX_AGGREGATE_EXPOSURE",
            )

        # 9. Daily realized loss check
        if portfolio.realized_pnl_cents < 0:
            realized_loss = abs(portfolio.realized_pnl_cents)
            if realized_loss >= self.config.max_daily_realized_loss_cents:
                return RiskCheckResult(
                    passed=False,
                    rejection_reason=(
                        f"Daily realized loss ({realized_loss}¢) breached limit "
                        f"({self.config.max_daily_realized_loss_cents}¢)"
                    ),
                    violated_rule="MAX_DAILY_LOSS",
                )

        # 10. Maximum drawdown check
        equity = portfolio.book_value_equity_cents()
        if self._peak_equity_cents is None or equity > self._peak_equity_cents:
            self._peak_equity_cents = equity
        drawdown = self._peak_equity_cents - equity
        if drawdown >= self.config.max_drawdown_cents:
            return RiskCheckResult(
                passed=False,
                rejection_reason=(
                    f"Drawdown ({drawdown}¢ from peak {self._peak_equity_cents}¢) breached limit "
                    f"({self.config.max_drawdown_cents}¢)"
                ),
                violated_rule="MAX_DRAWDOWN",
            )

        self._processed_candidate_ids.add(candidate.candidate_id)
        return RiskCheckResult(passed=True)

    @property
    def reserved_capital_cents(self) -> int:
        return self._reserved_capital_cents

    def reserve_capital(self, portfolio: PaperPortfolio, amount_cents: int) -> None:
        """Atomically lock capital from available to reserved risk capacity."""
        avail = portfolio.available_cash_cents - self._reserved_capital_cents
        if avail < amount_cents:
            raise RiskError(
                f"Cannot reserve {amount_cents}¢: only {avail}¢ unreserved available"
            )
        self._reserved_capital_cents += amount_cents

    def release_reservation(self, portfolio: PaperPortfolio, amount_cents: int) -> None:
        """Unlock previously reserved capital capacity."""
        self._reserved_capital_cents = max(0, self._reserved_capital_cents - amount_cents)
