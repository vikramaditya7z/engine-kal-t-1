"""Deterministic execution pricing and liquidity-aware arbitrage evaluation.

V3 Execution Pricing evaluates whether theoretical arbitrage opportunities
remain profitable and supported when executed against actual order-book depth.

Governing Principles:
1. Observed data establishes available depth and bid prices.
2. In Kalshi binary markets, available asks are derived from resting complementary bids:
   - To BUY YES: consume YES asks derived from NO bids (yes_ask = 100 - no_bid).
   - To BUY NO: consume NO asks derived from YES bids (no_ask = 100 - yes_bid).
3. Depth traversal strictly respects price priority: cheapest asks are consumed first.
4. Guaranteed payouts and gross profits are evaluated across all declared event states
   using the minimum (worst-case) outcome.
5. Exact arithmetic: prices, payouts, costs, and fees use exact integer cents and
   Decimal dollars. Binary floating-point arithmetic is strictly prohibited.
6. Honest status reporting: insufficient depth, partial fills, and unverified fees
   are explicitly flagged and never disguised as full or net-profitable execution.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .arbitrage import (
    ArbitrageOpportunity,
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
    OPPORTUNITY_PORTFOLIO,
    cents_to_dollars,
    dollars_to_cents,
)
from .contract import BinaryContract, ContractInputError, NO, YES
from .market_data import NormalizedMarket, NormalizedOrderBook, OrderBookLevel
from .portfolio import Event, Portfolio, Position, portfolio_payouts_cents


class ExecutionError(ValueError):
    """Raised when an execution-pricing input is invalid, malformed, or contradictory."""


# Execution status classifications
STATUS_DEPTH_SUPPORTED_NET_PROFITABLE = "DEPTH_SUPPORTED_NET_PROFITABLE"
STATUS_DEPTH_SUPPORTED_GROSS_POSITIVE = "DEPTH_SUPPORTED_GROSS_POSITIVE"
STATUS_DEPTH_SUPPORTED_NET_UNPROFITABLE = "DEPTH_SUPPORTED_NET_UNPROFITABLE"
STATUS_PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
STATUS_INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
STATUS_NO_GROSS_EDGE = "NO_GROSS_EDGE"
STATUS_INVALID_INPUT = "INVALID_INPUT"

_EXECUTION_DISCLAIMER = (
    "Execution pricing reflects static order-book snapshots. Live fills are subject "
    "to latency, race conditions, queue priority, and market movement across legs.",
)


@dataclass(frozen=True)
class AskLevel:
    """An exact derived ask level available to buy a specified side."""

    side: str
    price_cents: int
    price_dollars: Decimal
    quantity: int
    source_bid_cents: int

    def __post_init__(self) -> None:
        if self.side not in (YES, NO):
            raise ExecutionError(f"side must be {YES!r} or {NO!r}")
        if isinstance(self.price_cents, bool) or not isinstance(self.price_cents, int):
            raise ExecutionError("price_cents must be an integer")
        if not 0 <= self.price_cents <= 100:
            raise ExecutionError("price_cents must be between 0 and 100 cents")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise ExecutionError("quantity must be an integer")
        if self.quantity <= 0:
            raise ExecutionError("quantity must be positive")


@dataclass(frozen=True)
class ConsumedLevel:
    """A slice of an order-book level consumed during depth traversal."""

    price_cents: int
    price_dollars: Decimal
    quantity: int
    cost_cents: int
    cost_dollars: Decimal


@dataclass(frozen=True)
class DepthTraversalResult:
    """The result of traversing order-book depth for a requested purchase quantity."""

    market_ticker: str
    side: str
    requested_quantity: int
    supported_quantity: int
    unfilled_quantity: int
    is_full_fill: bool
    is_partial_fill: bool
    is_empty_fill: bool
    total_cost_cents: int
    total_cost_dollars: Decimal
    average_price_cents: Optional[Decimal]
    average_price_dollars: Optional[Decimal]
    consumed_levels: Tuple[ConsumedLevel, ...]
    status: str
    rejection_reason: Optional[str] = None


@dataclass(frozen=True)
class ExecutionPricingResult:
    """Structured result of evaluating an arbitrage candidate against book depth."""

    strategy_type: str
    status: str
    is_depth_supported: bool
    is_gross_profitable: bool
    is_net_profitable: Optional[bool]
    requested_quantity: int
    supported_quantity: int
    unfilled_quantity: int
    total_cost_cents: int
    total_cost_dollars: Decimal
    guaranteed_payout_cents: int
    guaranteed_payout_dollars: Decimal
    gross_profit_cents: int
    gross_profit_dollars: Decimal
    estimated_fees_cents: Optional[int] = None
    estimated_fees_dollars: Optional[Decimal] = None
    net_profit_cents: Optional[int] = None
    net_profit_dollars: Optional[Decimal] = None
    payouts_by_outcome: Dict[str, int] = ()
    profits_by_outcome: Dict[str, int] = ()
    leg_traversals: Tuple[DepthTraversalResult, ...] = ()
    rejection_reason: Optional[str] = None
    qualification_notes: Tuple[str, ...] = _EXECUTION_DISCLAIMER


def _require_int_quantity(value: object, name: str = "quantity") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionError(f"{name} must be an integer")
    if value <= 0:
        raise ExecutionError(f"{name} must be positive")
    return value


def _optional_fee_per_contract(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionError("fee_per_contract_cents must be an integer or None")
    if value < 0:
        raise ExecutionError("fee_per_contract_cents must be non-negative")
    return value


def derive_ask_levels(book: NormalizedOrderBook, side: str) -> Tuple[AskLevel, ...]:
    """Derive ask levels for the requested side from the resting complementary bids.

    In Kalshi binary markets:
    - Buying YES consumes YES asks, derived from NO bids:
      yes_ask_price = 100 - no_bid_price
      yes_ask_quantity = no_bid_quantity
    - Buying NO consumes NO asks, derived from YES bids:
      no_ask_price = 100 - yes_bid_price
      no_ask_quantity = yes_bid_quantity

    Returns ask levels sorted in ascending price order (cheapest ask first).
    """
    if not isinstance(book, NormalizedOrderBook):
        raise ExecutionError("book must be a NormalizedOrderBook instance")
    if side not in (YES, NO):
        raise ExecutionError(f"side must be {YES!r} or {NO!r}")

    # Complementary bids: YES asks come from NO bids; NO asks come from YES bids
    bids = book.no_bids if side == YES else book.yes_bids

    derived_asks: List[AskLevel] = []
    for bid in bids:
        bid_cents = dollars_to_cents(bid.price_dollars)
        ask_cents = 100 - bid_cents
        qty = int(bid.quantity)
        if qty <= 0:
            continue
        derived_asks.append(
            AskLevel(
                side=side,
                price_cents=ask_cents,
                price_dollars=cents_to_dollars(ask_cents),
                quantity=qty,
                source_bid_cents=bid_cents,
            )
        )

    # Sort asks by price ascending (cheapest first).
    # Since bids were sorted ascending, their complementary prices (100 - bid)
    # naturally sort cheapest when bids were highest.
    derived_asks.sort(key=lambda lvl: lvl.price_cents)
    return tuple(derived_asks)


def traverse_order_book_depth(
    book: NormalizedOrderBook,
    side: str,
    requested_quantity: int,
) -> DepthTraversalResult:
    """Traverse available order-book asks for the requested side and quantity.

    Consumes liquidity from cheapest ask to most expensive ask, respecting
    price priority and level capacity.
    """
    if not isinstance(book, NormalizedOrderBook):
        raise ExecutionError("book must be a NormalizedOrderBook instance")
    qty = _require_int_quantity(requested_quantity, "requested_quantity")
    asks = derive_ask_levels(book, side)

    consumed: List[ConsumedLevel] = []
    filled = 0
    total_cost_cents = 0
    remaining = qty

    for ask in asks:
        if remaining <= 0:
            break
        take = min(remaining, ask.quantity)
        cost_cents = take * ask.price_cents
        consumed.append(
            ConsumedLevel(
                price_cents=ask.price_cents,
                price_dollars=ask.price_dollars,
                quantity=take,
                cost_cents=cost_cents,
                cost_dollars=cents_to_dollars(cost_cents),
            )
        )
        filled += take
        total_cost_cents += cost_cents
        remaining -= take

    unfilled = qty - filled
    is_full = filled == qty
    is_partial = 0 < filled < qty
    is_empty = filled == 0

    avg_price_cents: Optional[Decimal] = None
    avg_price_dollars: Optional[Decimal] = None
    if filled > 0:
        avg_price_cents = Decimal(total_cost_cents) / Decimal(filled)
        avg_price_dollars = cents_to_dollars(total_cost_cents) / Decimal(filled)

    if is_full:
        status = "FULL_FILL"
        rejection_reason = None
    elif is_partial:
        status = "PARTIAL_FILL"
        rejection_reason = (
            f"Insufficient depth for market {book.market_ticker!r} {side}: "
            f"requested {qty}, but only {filled} available"
        )
    else:
        status = "INSUFFICIENT_DEPTH"
        rejection_reason = f"No ask liquidity available for market {book.market_ticker!r} {side}"

    return DepthTraversalResult(
        market_ticker=book.market_ticker,
        side=side,
        requested_quantity=qty,
        supported_quantity=filled,
        unfilled_quantity=unfilled,
        is_full_fill=is_full,
        is_partial_fill=is_partial,
        is_empty_fill=is_empty,
        total_cost_cents=total_cost_cents,
        total_cost_dollars=cents_to_dollars(total_cost_cents),
        average_price_cents=avg_price_cents,
        average_price_dollars=avg_price_dollars,
        consumed_levels=tuple(consumed),
        status=status,
        rejection_reason=rejection_reason,
    )


def evaluate_binary_parity_execution(
    book: NormalizedOrderBook,
    requested_quantity: int = 1,
    fee_per_contract_cents: Optional[int] = None,
) -> ExecutionPricingResult:
    """Evaluate single-market binary parity (1 YES + 1 NO) against book depth.

    Traverses both YES and NO asks for the requested quantity. If available depth
    differs between sides, identifies the common supported quantity, re-traverses
    at that exact quantity, and evaluates gross and fee-adjusted profitability.
    """
    if not isinstance(book, NormalizedOrderBook):
        raise ExecutionError("book must be a NormalizedOrderBook instance")
    qty = _require_int_quantity(requested_quantity, "requested_quantity")
    fee_cents = _optional_fee_per_contract(fee_per_contract_cents)

    # Initial traversal at full requested quantity
    trav_yes = traverse_order_book_depth(book, YES, qty)
    trav_no = traverse_order_book_depth(book, NO, qty)

    common_qty = min(trav_yes.supported_quantity, trav_no.supported_quantity)

    # If common quantity is 0, execution cannot proceed
    if common_qty == 0:
        missing_sides = []
        if trav_yes.supported_quantity == 0:
            missing_sides.append(f"{YES} (no asks)")
        if trav_no.supported_quantity == 0:
            missing_sides.append(f"{NO} (no asks)")
        reason = f"Insufficient depth: cannot form complete binary pairs ({', '.join(missing_sides)})"
        return _build_execution_result(
            strategy_type=OPPORTUNITY_BINARY_PARITY,
            status=STATUS_INSUFFICIENT_DEPTH,
            requested_quantity=qty,
            supported_quantity=0,
            total_cost_cents=0,
            guaranteed_payout_cents=0,
            payouts_by_outcome={YES: 0, NO: 0},
            total_contracts_filled=0,
            leg_traversals=(trav_yes, trav_no),
            fee_per_contract_cents=fee_cents,
            rejection_reason=reason,
        )

    # If common quantity is less than requested, re-traverse exactly at common_qty
    # so costs accurately reflect the consumed top-of-book levels for that quantity
    if common_qty < qty:
        trav_yes = traverse_order_book_depth(book, YES, common_qty)
        trav_no = traverse_order_book_depth(book, NO, common_qty)

    total_cost_cents = trav_yes.total_cost_cents + trav_no.total_cost_cents
    guaranteed_payout_cents = common_qty * 100
    payouts = {YES: guaranteed_payout_cents, NO: guaranteed_payout_cents}
    total_contracts = common_qty * 2

    status = (
        STATUS_PARTIALLY_SUPPORTED
        if common_qty < qty
        else STATUS_DEPTH_SUPPORTED_GROSS_POSITIVE
    )
    reason = (
        f"Partially supported: requested {qty} pairs, but order book supports only {common_qty} pairs"
        if common_qty < qty
        else None
    )

    return _build_execution_result(
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        status=status,
        requested_quantity=qty,
        supported_quantity=common_qty,
        total_cost_cents=total_cost_cents,
        guaranteed_payout_cents=guaranteed_payout_cents,
        payouts_by_outcome=payouts,
        total_contracts_filled=total_contracts,
        leg_traversals=(trav_yes, trav_no),
        fee_per_contract_cents=fee_cents,
        rejection_reason=reason,
    )


def evaluate_mece_basket_execution(
    event: Event,
    books_by_outcome: Mapping[str, NormalizedOrderBook],
    basket_side: str = YES,
    requested_quantity: int = 1,
    fee_per_contract_cents: Optional[int] = None,
) -> ExecutionPricingResult:
    """Evaluate an MECE event basket against depth from each outcome's order book.

    - If basket_side == YES: buys 1 YES contract on each outcome. Guaranteed payout = Q * 100¢.
    - If basket_side == NO: buys 1 NO contract on each outcome. Guaranteed payout = Q * (N - 1) * 100¢.
    """
    if not isinstance(event, Event):
        raise ExecutionError("event must be an Event instance")
    if not isinstance(books_by_outcome, Mapping):
        raise ExecutionError("books_by_outcome must be a mapping of outcome -> NormalizedOrderBook")
    if basket_side not in (YES, NO):
        raise ExecutionError(f"basket_side must be {YES!r} or {NO!r}")
    qty = _require_int_quantity(requested_quantity, "requested_quantity")
    fee_cents = _optional_fee_per_contract(fee_per_contract_cents)

    strat_type = (
        OPPORTUNITY_MECE_BASKET_LONG_YES
        if basket_side == YES
        else OPPORTUNITY_MECE_BASKET_LONG_NO
    )

    if not event.relationship_established:
        return _build_execution_result(
            strategy_type=strat_type,
            status=STATUS_INVALID_INPUT,
            requested_quantity=qty,
            supported_quantity=0,
            total_cost_cents=0,
            guaranteed_payout_cents=0,
            payouts_by_outcome={o: 0 for o in event.outcomes},
            total_contracts_filled=0,
            leg_traversals=(),
            fee_per_contract_cents=fee_cents,
            rejection_reason=(
                "Undeclared outcome relationship: event must have relationship_established=True "
                "(mutually exclusive and collectively exhaustive relationship is not established)"
            ),
        )

    missing_outcomes = [o for o in event.outcomes if o not in books_by_outcome]
    if missing_outcomes:
        return _build_execution_result(
            strategy_type=strat_type,
            status=STATUS_INVALID_INPUT,
            requested_quantity=qty,
            supported_quantity=0,
            total_cost_cents=0,
            guaranteed_payout_cents=0,
            payouts_by_outcome={o: 0 for o in event.outcomes},
            total_contracts_filled=0,
            leg_traversals=(),
            fee_per_contract_cents=fee_cents,
            rejection_reason=f"Incomplete basket: order book missing for outcome(s): {missing_outcomes}",
        )

    for outcome, book in books_by_outcome.items():
        if not isinstance(book, NormalizedOrderBook):
            raise ExecutionError(f"value for outcome {outcome!r} must be a NormalizedOrderBook instance")

    # Initial traversal at full requested quantity
    initial_traversals = [
        traverse_order_book_depth(books_by_outcome[outcome], basket_side, qty)
        for outcome in event.outcomes
    ]

    common_qty = min(t.supported_quantity for t in initial_traversals)

    if common_qty == 0:
        unfunded_outcomes = [
            t.market_ticker for t in initial_traversals if t.supported_quantity == 0
        ]
        return _build_execution_result(
            strategy_type=strat_type,
            status=STATUS_INSUFFICIENT_DEPTH,
            requested_quantity=qty,
            supported_quantity=0,
            total_cost_cents=0,
            guaranteed_payout_cents=0,
            payouts_by_outcome={o: 0 for o in event.outcomes},
            total_contracts_filled=0,
            leg_traversals=tuple(initial_traversals),
            fee_per_contract_cents=fee_cents,
            rejection_reason=f"Insufficient depth on market(s): {unfunded_outcomes}",
        )

    # Re-traverse at exact common quantity if partial
    final_traversals = (
        initial_traversals
        if common_qty == qty
        else [
            traverse_order_book_depth(books_by_outcome[outcome], basket_side, common_qty)
            for outcome in event.outcomes
        ]
    )

    total_cost_cents = sum(t.total_cost_cents for t in final_traversals)
    num_outcomes = len(event.outcomes)
    if basket_side == YES:
        guaranteed_payout_cents = common_qty * 100
        payouts = {o: guaranteed_payout_cents for o in event.outcomes}
    else:
        guaranteed_payout_cents = common_qty * (num_outcomes - 1) * 100
        payouts = {o: guaranteed_payout_cents for o in event.outcomes}

    total_contracts = common_qty * num_outcomes
    status = (
        STATUS_PARTIALLY_SUPPORTED
        if common_qty < qty
        else STATUS_DEPTH_SUPPORTED_GROSS_POSITIVE
    )
    reason = (
        f"Partially supported: requested {qty} units, but basket supports only {common_qty} units"
        if common_qty < qty
        else None
    )

    return _build_execution_result(
        strategy_type=strat_type,
        status=status,
        requested_quantity=qty,
        supported_quantity=common_qty,
        total_cost_cents=total_cost_cents,
        guaranteed_payout_cents=guaranteed_payout_cents,
        payouts_by_outcome=payouts,
        total_contracts_filled=total_contracts,
        leg_traversals=tuple(final_traversals),
        fee_per_contract_cents=fee_cents,
        rejection_reason=reason,
    )


def evaluate_portfolio_execution(
    portfolio: Portfolio,
    books_by_contract: Mapping[str, NormalizedOrderBook],
    unit_quantity: int = 1,
    fee_per_contract_cents: Optional[int] = None,
) -> ExecutionPricingResult:
    """Evaluate an arbitrary Portfolio against depth from order books.

    unit_quantity specifies how many units of the portfolio's position vector
    to acquire. For each position with base quantity Q_i, requires unit_quantity * Q_i.
    Identifies the maximum common unit scale supported across all legs.
    """
    if not isinstance(portfolio, Portfolio):
        raise ExecutionError("portfolio must be a Portfolio instance")
    if not isinstance(books_by_contract, Mapping):
        raise ExecutionError("books_by_contract must be a mapping of contract_id -> NormalizedOrderBook")
    unit_qty = _require_int_quantity(unit_quantity, "unit_quantity")
    fee_cents = _optional_fee_per_contract(fee_per_contract_cents)

    if not portfolio.positions:
        raise ExecutionError("portfolio must contain at least one position")

    if not portfolio.event.relationship_established:
        return _build_execution_result(
            strategy_type=OPPORTUNITY_PORTFOLIO,
            status=STATUS_INVALID_INPUT,
            requested_quantity=unit_qty,
            supported_quantity=0,
            total_cost_cents=0,
            guaranteed_payout_cents=0,
            payouts_by_outcome={o: 0 for o in portfolio.event.outcomes},
            total_contracts_filled=0,
            leg_traversals=(),
            fee_per_contract_cents=fee_cents,
            rejection_reason=(
                "Undeclared outcome relationship: event must have relationship_established=True "
                "(mutually exclusive and collectively exhaustive relationship is not established)"
            ),
        )

    for pos in portfolio.positions:
        if pos.contract_id not in books_by_contract:
            return _build_execution_result(
                strategy_type=OPPORTUNITY_PORTFOLIO,
                status=STATUS_INVALID_INPUT,
                requested_quantity=unit_qty,
                supported_quantity=0,
                total_cost_cents=0,
                guaranteed_payout_cents=0,
                payouts_by_outcome={o: 0 for o in portfolio.event.outcomes},
                total_contracts_filled=0,
                leg_traversals=(),
                fee_per_contract_cents=fee_cents,
                rejection_reason=f"Order book missing for contract {pos.contract_id!r}",
            )
        if not isinstance(books_by_contract[pos.contract_id], NormalizedOrderBook):
            raise ExecutionError(f"book for contract {pos.contract_id!r} must be a NormalizedOrderBook instance")

    # Initial traversal at full requested quantity
    initial_traversals = []
    supported_units = []
    for pos in portfolio.positions:
        needed_contracts = unit_qty * pos.quantity
        book = books_by_contract[pos.contract_id]
        trav = traverse_order_book_depth(book, pos.side, needed_contracts)
        initial_traversals.append(trav)
        supported_units.append(trav.supported_quantity // pos.quantity)

    common_units = min(supported_units)

    if common_units == 0:
        unfunded = [
            t.market_ticker for t in initial_traversals if t.supported_quantity == 0
        ]
        return _build_execution_result(
            strategy_type=OPPORTUNITY_PORTFOLIO,
            status=STATUS_INSUFFICIENT_DEPTH,
            requested_quantity=unit_qty,
            supported_quantity=0,
            total_cost_cents=0,
            guaranteed_payout_cents=0,
            payouts_by_outcome={o: 0 for o in portfolio.event.outcomes},
            total_contracts_filled=0,
            leg_traversals=tuple(initial_traversals),
            fee_per_contract_cents=fee_cents,
            rejection_reason=f"Insufficient depth for position(s): {unfunded}",
        )

    # Re-traverse at exact common units
    final_traversals = []
    for pos in portfolio.positions:
        actual_contracts = common_units * pos.quantity
        book = books_by_contract[pos.contract_id]
        trav = traverse_order_book_depth(book, pos.side, actual_contracts)
        final_traversals.append(trav)

    total_cost_cents = sum(t.total_cost_cents for t in final_traversals)

    # Build scaled positions to compute exact payoffs across all event states
    scaled_positions = tuple(
        Position(contract_id=pos.contract_id, side=pos.side, quantity=common_units * pos.quantity)
        for pos in portfolio.positions
    )
    scaled_portfolio = Portfolio(
        event=portfolio.event,
        contracts=portfolio.contracts,
        positions=scaled_positions,
    )
    try:
        payouts = portfolio_payouts_cents(scaled_portfolio)
    except ContractInputError as exc:
        raise ExecutionError(f"portfolio payout calculation failed: {exc}") from exc

    guaranteed_payout_cents = min(payouts.values()) if payouts else 0
    total_contracts_filled = sum(p.quantity for p in scaled_positions)

    status = (
        STATUS_PARTIALLY_SUPPORTED
        if common_units < unit_qty
        else STATUS_DEPTH_SUPPORTED_GROSS_POSITIVE
    )
    reason = (
        f"Partially supported: requested {unit_qty} units, but depth supports only {common_units} units"
        if common_units < unit_qty
        else None
    )

    return _build_execution_result(
        strategy_type=OPPORTUNITY_PORTFOLIO,
        status=status,
        requested_quantity=unit_qty,
        supported_quantity=common_units,
        total_cost_cents=total_cost_cents,
        guaranteed_payout_cents=guaranteed_payout_cents,
        payouts_by_outcome=payouts,
        total_contracts_filled=total_contracts_filled,
        leg_traversals=tuple(final_traversals),
        fee_per_contract_cents=fee_cents,
        rejection_reason=reason,
    )


def _build_execution_result(
    strategy_type: str,
    status: str,
    requested_quantity: int,
    supported_quantity: int,
    total_cost_cents: int,
    guaranteed_payout_cents: int,
    payouts_by_outcome: Dict[str, int],
    total_contracts_filled: int,
    leg_traversals: Tuple[DepthTraversalResult, ...],
    fee_per_contract_cents: Optional[int],
    rejection_reason: Optional[str] = None,
) -> ExecutionPricingResult:
    gross_profit_cents = guaranteed_payout_cents - total_cost_cents
    is_gross_profitable = gross_profit_cents > 0 and supported_quantity > 0
    is_depth_supported = supported_quantity == requested_quantity and requested_quantity > 0
    unfilled = requested_quantity - supported_quantity

    profits_by_outcome = {
        outcome: payout - total_cost_cents
        for outcome, payout in payouts_by_outcome.items()
    }

    # Evaluate economic status if depth was sufficient or partially supported
    if supported_quantity > 0:
        if gross_profit_cents <= 0:
            status = STATUS_NO_GROSS_EDGE
            if gross_profit_cents == 0:
                rejection_reason = "Break-even: total acquisition cost equals guaranteed settlement payout"
            else:
                rejection_reason = (
                    f"No gross edge: total acquisition cost ({total_cost_cents}¢) exceeds "
                    f"guaranteed payout ({guaranteed_payout_cents}¢)"
                )

    est_fees_cents: Optional[int] = None
    est_fees_dollars: Optional[Decimal] = None
    net_profit_cents: Optional[int] = None
    net_profit_dollars: Optional[Decimal] = None
    is_net_profitable: Optional[bool] = None
    qualifications = list(_EXECUTION_DISCLAIMER)

    if fee_per_contract_cents is not None and supported_quantity > 0:
        est_fees_cents = total_contracts_filled * fee_per_contract_cents
        est_fees_dollars = cents_to_dollars(est_fees_cents)
        net_profit_cents = gross_profit_cents - est_fees_cents
        net_profit_dollars = cents_to_dollars(net_profit_cents)
        is_net_profitable = net_profit_cents > 0

        if is_depth_supported and is_gross_profitable:
            if is_net_profitable:
                status = STATUS_DEPTH_SUPPORTED_NET_PROFITABLE
            else:
                status = STATUS_DEPTH_SUPPORTED_NET_UNPROFITABLE
                qualifications.append(
                    "Candidate is depth-supported and gross-positive, but unprofitable after estimated fees."
                )
    else:
        if is_depth_supported and is_gross_profitable:
            status = STATUS_DEPTH_SUPPORTED_GROSS_POSITIVE
        qualifications.append("Exchange fees not provided; net profitability cannot be established.")

    return ExecutionPricingResult(
        strategy_type=strategy_type,
        status=status,
        is_depth_supported=is_depth_supported,
        is_gross_profitable=is_gross_profitable,
        is_net_profitable=is_net_profitable,
        requested_quantity=requested_quantity,
        supported_quantity=supported_quantity,
        unfilled_quantity=unfilled,
        total_cost_cents=total_cost_cents,
        total_cost_dollars=cents_to_dollars(total_cost_cents),
        guaranteed_payout_cents=guaranteed_payout_cents,
        guaranteed_payout_dollars=cents_to_dollars(guaranteed_payout_cents),
        gross_profit_cents=gross_profit_cents,
        gross_profit_dollars=cents_to_dollars(gross_profit_cents),
        estimated_fees_cents=est_fees_cents,
        estimated_fees_dollars=est_fees_dollars,
        net_profit_cents=net_profit_cents,
        net_profit_dollars=net_profit_dollars,
        payouts_by_outcome=payouts_by_outcome,
        profits_by_outcome=profits_by_outcome,
        leg_traversals=leg_traversals,
        rejection_reason=rejection_reason,
        qualification_notes=tuple(qualifications),
    )
