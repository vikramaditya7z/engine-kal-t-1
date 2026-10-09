"""Paper execution engine simulating trades against V3 order-book depth.

V4 Paper Execution accepts opportunities or candidate portfolios, evaluates
them against V3 order books, and returns fully auditable PaperTrade records.
"""

from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence, Tuple
import uuid

from .arbitrage import (
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
    OPPORTUNITY_PORTFOLIO,
    cents_to_dollars,
)
from .contract import NO, YES
from .execution import (
    STATUS_DEPTH_SUPPORTED_GROSS_POSITIVE,
    STATUS_DEPTH_SUPPORTED_NET_PROFITABLE,
    STATUS_DEPTH_SUPPORTED_NET_UNPROFITABLE,
    STATUS_INSUFFICIENT_DEPTH,
    STATUS_INVALID_INPUT,
    STATUS_NO_GROSS_EDGE,
    STATUS_PARTIALLY_SUPPORTED,
    DepthTraversalResult,
    ExecutionError,
    ExecutionPricingResult,
    evaluate_binary_parity_execution,
    evaluate_mece_basket_execution,
    evaluate_portfolio_execution,
)
from .market_data import NormalizedOrderBook
from .paper_trade import (
    PaperTrade,
    TradeLeg,
    TradeLifecycleError,
    TradeState,
    create_proposed_trade,
)
from .portfolio import Event, Portfolio


class PaperExecutionError(ValueError):
    """Raised when paper execution simulation encounters invalid parameters."""


def _generate_trade_id() -> str:
    return f"sim-{uuid.uuid4().hex[:12]}"


def _legs_from_traversals(
    traversals: Sequence[DepthTraversalResult],
    contract_ids: Optional[Sequence[str]] = None,
) -> Tuple[TradeLeg, ...]:
    legs = []
    for i, t in enumerate(traversals):
        cid = contract_ids[i] if contract_ids and i < len(contract_ids) else t.market_ticker
        legs.append(
            TradeLeg(
                contract_id=cid,
                market_ticker=t.market_ticker,
                side=t.side,
                requested_quantity=t.requested_quantity,
                filled_quantity=t.supported_quantity,
                total_cost_cents=t.total_cost_cents,
                total_cost_dollars=t.total_cost_dollars,
                average_price_cents=t.average_price_cents,
                average_price_dollars=t.average_price_dollars,
                consumed_levels=t.consumed_levels,
            )
        )
    return tuple(legs)


class PaperExecutionEngine:
    """Simulates multi-leg trade execution against V3 liquidity evaluation."""

    def __init__(self, *, default_fee_per_contract_cents: Optional[int] = None):
        self.default_fee_per_contract_cents = default_fee_per_contract_cents

    def execute_from_pricing_result(
        self,
        pricing_result: ExecutionPricingResult,
        *,
        trade_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        contract_ids: Optional[Sequence[str]] = None,
        enforce_net_profitability: bool = False,
        portfolio: Optional[Any] = None,
        risk_manager: Optional[Any] = None,
    ) -> PaperTrade:
        """Construct a simulated PaperTrade directly from a V3 ExecutionPricingResult."""
        if not isinstance(pricing_result, ExecutionPricingResult):
            raise PaperExecutionError("pricing_result must be an ExecutionPricingResult instance")

        tid = trade_id or _generate_trade_id()
        legs = _legs_from_traversals(pricing_result.leg_traversals, contract_ids)

        proposed = create_proposed_trade(
            trade_id=tid,
            strategy_type=pricing_result.strategy_type,
            requested_quantity=pricing_result.requested_quantity,
            created_at=timestamp,
            evaluated_at=timestamp,
            execution_result=pricing_result,
            legs=legs,
        )

        # Check for non-executable or unviable statuses
        if pricing_result.status in (
            STATUS_INVALID_INPUT,
            STATUS_INSUFFICIENT_DEPTH,
            STATUS_NO_GROSS_EDGE,
        ):
            reason = pricing_result.rejection_reason or f"Rejected due to {pricing_result.status}"
            return proposed.transition_to(
                TradeState.REJECTED,
                timestamp=timestamp,
                reason=reason,
            )

        if enforce_net_profitability and pricing_result.is_net_profitable is False:
            return proposed.transition_to(
                TradeState.REJECTED,
                timestamp=timestamp,
                reason="Unprofitable after deducting estimated exchange fees",
            )

        # Pre-trade risk check before accepting for simulation
        if risk_manager is not None and portfolio is not None:
            risk_verdict = risk_manager.evaluate_trade(portfolio, proposed)
            if not risk_verdict.passed:
                return proposed.transition_to(
                    TradeState.REJECTED,
                    timestamp=timestamp,
                    reason=f"Risk limit exceeded: {risk_verdict.rejection_reason}",
                )

        # Accept for simulation
        accepted = proposed.transition_to(
            TradeState.ACCEPTED,
            timestamp=timestamp,
            reason="Accepted for simulation",
        )

        # Determine fill outcome
        if pricing_result.status in (
            STATUS_DEPTH_SUPPORTED_NET_PROFITABLE,
            STATUS_DEPTH_SUPPORTED_GROSS_POSITIVE,
            STATUS_DEPTH_SUPPORTED_NET_UNPROFITABLE,
        ):
            # Complete fill of requested quantity
            return accepted.transition_to(
                TradeState.FILLED,
                timestamp=timestamp,
                reason="Full fill simulated against available depth",
                filled_quantity=pricing_result.supported_quantity,
                total_cost_cents=pricing_result.total_cost_cents,
                estimated_fees_cents=pricing_result.estimated_fees_cents or 0,
                guaranteed_payout_cents=pricing_result.guaranteed_payout_cents,
                legs=legs,
            )

        if pricing_result.status == STATUS_PARTIALLY_SUPPORTED:
            # Partial fill at common bottleneck quantity
            return accepted.transition_to(
                TradeState.PARTIALLY_FILLED,
                timestamp=timestamp,
                reason=(
                    f"Partial fill simulated: requested {pricing_result.requested_quantity}, "
                    f"depth supports {pricing_result.supported_quantity}"
                ),
                filled_quantity=pricing_result.supported_quantity,
                total_cost_cents=pricing_result.total_cost_cents,
                estimated_fees_cents=pricing_result.estimated_fees_cents or 0,
                guaranteed_payout_cents=pricing_result.guaranteed_payout_cents,
                legs=legs,
            )

        # Any unhandled status fails closed by rejecting
        return proposed.transition_to(
            TradeState.REJECTED,
            timestamp=timestamp,
            reason=f"Unrecognized execution status: {pricing_result.status}",
        )

    def simulate_binary_parity(
        self,
        book: NormalizedOrderBook,
        requested_quantity: int = 1,
        fee_per_contract_cents: Optional[int] = None,
        *,
        trade_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        enforce_net_profitability: bool = False,
        portfolio: Optional[Any] = None,
        risk_manager: Optional[Any] = None,
    ) -> PaperTrade:
        """Simulate execution of single-market binary parity against book depth."""
        fee = (
            fee_per_contract_cents
            if fee_per_contract_cents is not None
            else self.default_fee_per_contract_cents
        )
        pricing_result = evaluate_binary_parity_execution(
            book,
            requested_quantity=requested_quantity,
            fee_per_contract_cents=fee,
        )
        return self.execute_from_pricing_result(
            pricing_result,
            trade_id=trade_id,
            timestamp=timestamp,
            enforce_net_profitability=enforce_net_profitability,
            portfolio=portfolio,
            risk_manager=risk_manager,
        )

    def simulate_mece_basket(
        self,
        event: Event,
        books_by_outcome: Mapping[str, NormalizedOrderBook],
        basket_side: str = YES,
        requested_quantity: int = 1,
        fee_per_contract_cents: Optional[int] = None,
        *,
        trade_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        enforce_net_profitability: bool = False,
        portfolio: Optional[Any] = None,
        risk_manager: Optional[Any] = None,
    ) -> PaperTrade:
        """Simulate execution of an MECE event basket against depth from each outcome."""
        fee = (
            fee_per_contract_cents
            if fee_per_contract_cents is not None
            else self.default_fee_per_contract_cents
        )
        pricing_result = evaluate_mece_basket_execution(
            event,
            books_by_outcome,
            basket_side=basket_side,
            requested_quantity=requested_quantity,
            fee_per_contract_cents=fee,
        )
        return self.execute_from_pricing_result(
            pricing_result,
            trade_id=trade_id,
            timestamp=timestamp,
            enforce_net_profitability=enforce_net_profitability,
            portfolio=portfolio,
            risk_manager=risk_manager,
        )

    def simulate_portfolio(
        self,
        portfolio: Portfolio,
        books_by_contract: Mapping[str, NormalizedOrderBook],
        unit_quantity: int = 1,
        fee_per_contract_cents: Optional[int] = None,
        *,
        trade_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        enforce_net_profitability: bool = False,
        paper_portfolio: Optional[Any] = None,
        risk_manager: Optional[Any] = None,
    ) -> PaperTrade:
        """Simulate execution of an arbitrary Portfolio against depth from order books."""
        fee = (
            fee_per_contract_cents
            if fee_per_contract_cents is not None
            else self.default_fee_per_contract_cents
        )
        pricing_result = evaluate_portfolio_execution(
            portfolio,
            books_by_contract,
            unit_quantity=unit_quantity,
            fee_per_contract_cents=fee,
        )
        contract_ids = [pos.contract_id for pos in portfolio.positions]
        return self.execute_from_pricing_result(
            pricing_result,
            trade_id=trade_id,
            timestamp=timestamp,
            contract_ids=contract_ids,
            enforce_net_profitability=enforce_net_profitability,
            portfolio=paper_portfolio,
            risk_manager=risk_manager,
        )
