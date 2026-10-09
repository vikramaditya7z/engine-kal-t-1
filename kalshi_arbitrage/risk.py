"""Deterministic pre-trade risk controls and configurable exposure limits.

V4 Risk Controls ensure simulated trades respect capital, exposure,
position size, and liquidity limits before acceptance into the ledger.
"""

from dataclasses import dataclass
from typing import Optional, Set

from .ledger import PaperPortfolio
from .paper_trade import PaperTrade


class RiskError(ValueError):
    """Raised when risk configuration or check inputs are invalid."""


@dataclass(frozen=True)
class RiskCheckResult:
    """The structured verdict of evaluating a trade against pre-trade risk limits."""

    passed: bool
    rejection_reason: Optional[str] = None
    violated_rule: Optional[str] = None


@dataclass(frozen=True)
class RiskConfig:
    """Configurable boundaries and constraints for paper trading."""

    max_cost_per_trade_cents: int = 50_000  # $500.00 per trade
    max_requested_quantity: int = 1_000  # max 1,000 contracts requested
    max_position_size: int = 1_000  # max 1,000 contracts in any single instrument
    max_aggregate_exposure_cents: int = 200_000  # $2,000.00 total open position cost basis
    require_positive_gross_edge: bool = True
    require_positive_net_edge: bool = False
    allow_duplicate_trades: bool = False

    def __post_init__(self) -> None:
        if self.max_cost_per_trade_cents <= 0:
            raise RiskError("max_cost_per_trade_cents must be positive")
        if self.max_requested_quantity <= 0:
            raise RiskError("max_requested_quantity must be positive")
        if self.max_position_size <= 0:
            raise RiskError("max_position_size must be positive")
        if self.max_aggregate_exposure_cents <= 0:
            raise RiskError("max_aggregate_exposure_cents must be positive")


class RiskManager:
    """Enforces pre-trade risk checks before a trade can be accepted or simulated."""

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        self._processed_trade_ids: Set[str] = set()

    def evaluate_trade(
        self,
        portfolio: PaperPortfolio,
        trade: PaperTrade,
    ) -> RiskCheckResult:
        """Run all pre-trade checks on the proposed trade.

        Returns RiskCheckResult(passed=True) if all limits are satisfied,
        or RiskCheckResult(passed=False, rejection_reason=..., violated_rule=...)
        describing the first violated boundary.
        """
        if not isinstance(portfolio, PaperPortfolio):
            raise RiskError("portfolio must be a PaperPortfolio instance")
        if not isinstance(trade, PaperTrade):
            raise RiskError("trade must be a PaperTrade instance")

        # 1. Duplicate trade ID check
        if not self.config.allow_duplicate_trades:
            if (
                trade.trade_id in self._processed_trade_ids
                or trade.trade_id in portfolio.processed_trade_ids
            ):
                return RiskCheckResult(
                    passed=False,
                    rejection_reason=f"Duplicate trade: ID {trade.trade_id!r} has already been evaluated or processed",
                    violated_rule="DUPLICATE_TRADE_ID",
                )

        # 2. Maximum requested quantity check
        if trade.requested_quantity > self.config.max_requested_quantity:
            return RiskCheckResult(
                passed=False,
                rejection_reason=(
                    f"Requested quantity ({trade.requested_quantity}) exceeds maximum "
                    f"per-trade limit ({self.config.max_requested_quantity})"
                ),
                violated_rule="MAX_REQUESTED_QUANTITY",
            )

        # 3. Maximum cost per trade check
        cash_needed = trade.total_cash_required_cents
        if cash_needed > self.config.max_cost_per_trade_cents:
            return RiskCheckResult(
                passed=False,
                rejection_reason=(
                    f"Trade cash requirement ({cash_needed}¢) exceeds maximum "
                    f"per-trade limit ({self.config.max_cost_per_trade_cents}¢)"
                ),
                violated_rule="MAX_COST_PER_TRADE",
            )

        # 4. Available cash check
        if portfolio.available_cash_cents < cash_needed:
            return RiskCheckResult(
                passed=False,
                rejection_reason=(
                    f"Insufficient available cash: portfolio has {portfolio.available_cash_cents}¢, "
                    f"trade requires {cash_needed}¢"
                ),
                violated_rule="INSUFFICIENT_AVAILABLE_CASH",
            )

        # 5. Aggregate portfolio exposure check
        current_exposure = portfolio.total_cost_basis_cents()
        projected_exposure = current_exposure + trade.total_cost_cents
        if projected_exposure > self.config.max_aggregate_exposure_cents:
            return RiskCheckResult(
                passed=False,
                rejection_reason=(
                    f"Projected aggregate exposure ({projected_exposure}¢) exceeds limit "
                    f"({self.config.max_aggregate_exposure_cents}¢)"
                ),
                violated_rule="MAX_AGGREGATE_EXPOSURE",
            )

        # 6. Instrument position size check
        for leg in trade.legs:
            key = (leg.contract_id, leg.side)
            existing_qty = (
                portfolio.positions[key].quantity if key in portfolio.positions else 0
            )
            # Use filled_quantity if trade already evaluated, else requested_quantity
            qty_to_add = (
                leg.filled_quantity if leg.filled_quantity > 0 else leg.requested_quantity
            )
            projected_pos = existing_qty + qty_to_add
            if projected_pos > self.config.max_position_size:
                return RiskCheckResult(
                    passed=False,
                    rejection_reason=(
                        f"Projected position for {leg.contract_id} {leg.side} ({projected_pos}) "
                        f"exceeds instrument position limit ({self.config.max_position_size})"
                    ),
                    violated_rule="MAX_POSITION_SIZE",
                )

        # 7. Edge & profitability checks
        if trade.execution_result:
            if self.config.require_positive_gross_edge:
                if trade.execution_result.gross_profit_cents <= 0:
                    return RiskCheckResult(
                        passed=False,
                        rejection_reason="Opportunity does not have positive gross edge",
                        violated_rule="REQUIRE_POSITIVE_GROSS_EDGE",
                    )
            if self.config.require_positive_net_edge:
                if trade.execution_result.is_net_profitable is False:
                    return RiskCheckResult(
                        passed=False,
                        rejection_reason="Opportunity is not profitable after deducting estimated fees",
                        violated_rule="REQUIRE_POSITIVE_NET_EDGE",
                    )

        return RiskCheckResult(passed=True)

    def record_processed_trade(self, trade_id: str) -> None:
        """Mark trade_id as processed to prevent re-evaluation."""
        self._processed_trade_ids.add(trade_id)
