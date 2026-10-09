"""Deterministic arbitrage detection for Kalshi prediction markets.

V2 Arbitrage Detection evaluates mathematically valid arbitrage candidates
between appropriately related contracts using explicitly declared settlement
relationships and available market prices.

Key Invariants:
1. Deterministic calculations: all financial arithmetic uses exact integer
   cents for V0 model compatibility and exact Decimal representations for
   dollar values. Binary floating-point is strictly prohibited.
2. Explicit relationships: cross-contract arbitrage is evaluated ONLY when
   the mutually exclusive and collectively exhaustive (MECE) relationship
   between outcomes has been explicitly established. Relationships are never
   inferred from market titles, tickers, or metadata.
3. Guaranteed payout across all states: every candidate is evaluated across
   every declared outcome of the event. The guaranteed payout is the worst-case
   payout across all states.
4. Qualification of opportunities: candidates are categorized as gross
   theoretical arbitrage. Executability and net profitability are explicitly
   disclaimed unless fee and liquidity models are provided.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Mapping, Optional, Sequence, Tuple

from .contract import BinaryContract, ContractInputError, NO, YES
from .market_data import NormalizedMarket
from .portfolio import Event, Portfolio, Position, portfolio_payouts_cents


class ArbitrageInputError(ValueError):
    """Raised when an arbitrage detection input is invalid, malformed, or ambiguous."""


OPPORTUNITY_BINARY_PARITY = "binary_parity"
OPPORTUNITY_MECE_BASKET_LONG_YES = "mece_basket_long_yes"
OPPORTUNITY_MECE_BASKET_LONG_NO = "mece_basket_long_no"
OPPORTUNITY_PORTFOLIO = "portfolio"

_DEFAULT_QUALIFICATION = (
    "Gross theoretical candidate only. Executability and net profitability are unverified; "
    "order-book depth, exchange fees, and liquidity limits are not evaluated in V2.",
)


def dollars_to_cents(price_dollars: object) -> int:
    """Convert an exact Decimal dollar price in [0.00, 1.00] to integer cents [0, 100].

    Raises ArbitrageInputError if the value is not an exact Decimal, is out of bounds,
    or contains fractional cents (sub-cent precision) that cannot be mapped to the
    integer-cent model without precision loss.
    """
    if isinstance(price_dollars, (bool, float)):
        raise ArbitrageInputError("price_dollars must be an exact Decimal, not float or bool")
    if not isinstance(price_dollars, (Decimal, int, str)):
        raise ArbitrageInputError("price_dollars must be a Decimal, str, or int")
    if isinstance(price_dollars, str) and not price_dollars.strip():
        raise ArbitrageInputError("price_dollars cannot be empty")
    try:
        dec = Decimal(price_dollars) if not isinstance(price_dollars, Decimal) else price_dollars
    except Exception as exc:
        raise ArbitrageInputError("invalid price_dollars") from exc
    if not dec.is_finite():
        raise ArbitrageInputError("price_dollars must be finite")
    if dec < Decimal("0") or dec > Decimal("1"):
        raise ArbitrageInputError(f"price_dollars {dec} must be between 0.00 and 1.00")

    cents_dec = dec * Decimal("100")
    if cents_dec != cents_dec.to_integral_value():
        raise ArbitrageInputError(
            f"sub-cent price ${dec} cannot be converted to integer cents without precision loss"
        )
    return int(cents_dec)


def cents_to_dollars(cents: int) -> Decimal:
    """Convert an integer cents value to an exact Decimal dollar representation."""
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise ArbitrageInputError("cents must be an integer")
    return (Decimal(cents) / Decimal("100")).quantize(Decimal("0.01"))


def _require_int_cents(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArbitrageInputError(f"{name} must be an integer")
    if not 0 <= value <= 100:
        raise ArbitrageInputError(f"{name} must be between 0 and 100 cents")
    return value


def _require_positive_quantity(value: object, name: str = "quantity") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArbitrageInputError(f"{name} must be an integer")
    if value <= 0:
        raise ArbitrageInputError(f"{name} must be positive")
    return value


def _optional_fee_per_contract(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArbitrageInputError("fee_per_contract_cents must be an integer or None")
    if value < 0:
        raise ArbitrageInputError("fee_per_contract_cents must be non-negative")
    return value


@dataclass(frozen=True)
class ArbitrageOpportunity:
    """Detailed result of an arbitrage candidate evaluation."""

    opportunity_type: str
    is_arbitrage: bool
    guaranteed_payout_cents: int
    total_cost_cents: int
    gross_edge_cents: int
    payouts_by_outcome: Dict[str, int]
    profits_by_outcome: Dict[str, int]
    guaranteed_payout_dollars: Decimal
    total_cost_dollars: Decimal
    gross_edge_dollars: Decimal
    is_executable: bool = False
    is_net_profitable: Optional[bool] = None
    net_edge_cents: Optional[int] = None
    net_edge_dollars: Optional[Decimal] = None
    estimated_fees_cents: Optional[int] = None
    rejection_reason: Optional[str] = None
    qualification_notes: Tuple[str, ...] = _DEFAULT_QUALIFICATION
    contract_ids: Tuple[str, ...] = ()


def _build_opportunity(
    opportunity_type: str,
    guaranteed_payout_cents: int,
    total_cost_cents: int,
    payouts_by_outcome: Dict[str, int],
    total_contracts: int,
    contract_ids: Tuple[str, ...],
    fee_per_contract_cents: Optional[int],
    rejection_reason: Optional[str] = None,
) -> ArbitrageOpportunity:
    gross_edge_cents = guaranteed_payout_cents - total_cost_cents
    is_arbitrage = gross_edge_cents > 0 and rejection_reason is None

    profits_by_outcome = {
        outcome: payout - total_cost_cents
        for outcome, payout in payouts_by_outcome.items()
    }

    if rejection_reason is None:
        if gross_edge_cents == 0:
            rejection_reason = "Break-even: gross edge is zero (total cost equals guaranteed payout)"
        elif gross_edge_cents < 0:
            min_outcome = min(profits_by_outcome, key=profits_by_outcome.get) if profits_by_outcome else "unknown"
            min_profit = profits_by_outcome[min_outcome] if profits_by_outcome else gross_edge_cents
            rejection_reason = (
                f"No arbitrage: total cost ({total_cost_cents}¢) exceeds guaranteed payout ({guaranteed_payout_cents}¢); "
                f"worst-case outcome '{min_outcome}' yields {min_profit}¢"
            )

    net_edge_cents: Optional[int] = None
    net_edge_dollars: Optional[Decimal] = None
    estimated_fees_cents: Optional[int] = None
    is_net_profitable: Optional[bool] = None
    qualification_notes = list(_DEFAULT_QUALIFICATION)

    if fee_per_contract_cents is not None:
        estimated_fees_cents = total_contracts * fee_per_contract_cents
        net_edge_cents = gross_edge_cents - estimated_fees_cents
        net_edge_dollars = cents_to_dollars(net_edge_cents)
        is_net_profitable = net_edge_cents > 0
        if not is_net_profitable:
            qualification_notes.append("Opportunity has positive gross edge but is unprofitable after estimated fees.")
    else:
        qualification_notes.append("Exchange fees not provided; net profitability cannot be established.")

    return ArbitrageOpportunity(
        opportunity_type=opportunity_type,
        is_arbitrage=is_arbitrage,
        guaranteed_payout_cents=guaranteed_payout_cents,
        total_cost_cents=total_cost_cents,
        gross_edge_cents=gross_edge_cents,
        payouts_by_outcome=payouts_by_outcome,
        profits_by_outcome=profits_by_outcome,
        guaranteed_payout_dollars=cents_to_dollars(guaranteed_payout_cents),
        total_cost_dollars=cents_to_dollars(total_cost_cents),
        gross_edge_dollars=cents_to_dollars(gross_edge_cents),
        is_executable=False,
        is_net_profitable=is_net_profitable,
        net_edge_cents=net_edge_cents,
        net_edge_dollars=net_edge_dollars,
        estimated_fees_cents=estimated_fees_cents,
        rejection_reason=rejection_reason,
        qualification_notes=tuple(qualification_notes),
        contract_ids=contract_ids,
    )


def evaluate_binary_parity(
    market_id: str,
    yes_price_cents: int,
    no_price_cents: int,
    quantity: int = 1,
    fee_per_contract_cents: Optional[int] = None,
) -> ArbitrageOpportunity:
    """Evaluate single-market binary complement parity (buying YES and buying NO).

    In any valid binary market, exactly one side settles to 100¢ and the other to 0¢.
    Acquiring 1 YES and 1 NO guarantees a 100¢ settlement payout.
    """
    if not isinstance(market_id, str) or not market_id.strip():
        raise ArbitrageInputError("market_id must be a non-empty string")
    yes_cents = _require_int_cents(yes_price_cents, "yes_price_cents")
    no_cents = _require_int_cents(no_price_cents, "no_price_cents")
    qty = _require_positive_quantity(quantity, "quantity")
    fee_cents = _optional_fee_per_contract(fee_per_contract_cents)

    cost_per_pair = yes_cents + no_cents
    total_cost_cents = qty * cost_per_pair
    guaranteed_payout_cents = qty * 100

    payouts_by_outcome = {
        YES: guaranteed_payout_cents,
        NO: guaranteed_payout_cents,
    }

    return _build_opportunity(
        opportunity_type=OPPORTUNITY_BINARY_PARITY,
        guaranteed_payout_cents=guaranteed_payout_cents,
        total_cost_cents=total_cost_cents,
        payouts_by_outcome=payouts_by_outcome,
        total_contracts=qty * 2,
        contract_ids=(market_id,),
        fee_per_contract_cents=fee_cents,
    )


def evaluate_market_parity(
    market: NormalizedMarket,
    quantity: int = 1,
    fee_per_contract_cents: Optional[int] = None,
) -> ArbitrageOpportunity:
    """Evaluate binary complement parity for a NormalizedMarket object using available ask quotes."""
    if not isinstance(market, NormalizedMarket):
        raise ArbitrageInputError("market must be a NormalizedMarket instance")
    if market.market_type != "binary":
        raise ArbitrageInputError(f"only binary markets are supported, got {market.market_type!r}")

    if market.yes_ask_dollars is None or market.no_ask_dollars is None:
        missing = []
        if market.yes_ask_dollars is None:
            missing.append("yes_ask_dollars")
        if market.no_ask_dollars is None:
            missing.append("no_ask_dollars")
        return _build_opportunity(
            opportunity_type=OPPORTUNITY_BINARY_PARITY,
            guaranteed_payout_cents=0,
            total_cost_cents=0,
            payouts_by_outcome={YES: 0, NO: 0},
            total_contracts=quantity * 2 if isinstance(quantity, int) and quantity > 0 else 0,
            contract_ids=(market.ticker,),
            fee_per_contract_cents=None,
            rejection_reason=f"Incomplete market quotes: missing {', '.join(missing)}",
        )

    yes_cents = dollars_to_cents(market.yes_ask_dollars)
    no_cents = dollars_to_cents(market.no_ask_dollars)

    return evaluate_binary_parity(
        market_id=market.ticker,
        yes_price_cents=yes_cents,
        no_price_cents=no_cents,
        quantity=quantity,
        fee_per_contract_cents=fee_per_contract_cents,
    )


def evaluate_mece_event_basket(
    event: Event,
    outcome_prices_cents: Mapping[str, int],
    basket_side: str = YES,
    quantity: int = 1,
    fee_per_contract_cents: Optional[int] = None,
) -> ArbitrageOpportunity:
    """Evaluate an event basket across an explicitly established MECE event.

    - If basket_side == "YES": buys 1 YES contract for each outcome in event.outcomes.
      Guaranteed payout is 100¢ * quantity (since exactly one outcome occurs).
    - If basket_side == "NO": buys 1 NO contract for each outcome in event.outcomes.
      Guaranteed payout is 100¢ * quantity * (len(outcomes) - 1).
    """
    if not isinstance(event, Event):
        raise ArbitrageInputError("event must be an Event instance")
    if not isinstance(outcome_prices_cents, Mapping):
        raise ArbitrageInputError("outcome_prices_cents must be a mapping")
    if basket_side not in (YES, NO):
        raise ArbitrageInputError(f"basket_side must be {YES!r} or {NO!r}")
    qty = _require_positive_quantity(quantity, "quantity")
    fee_cents = _optional_fee_per_contract(fee_per_contract_cents)

    opp_type = (
        OPPORTUNITY_MECE_BASKET_LONG_YES
        if basket_side == YES
        else OPPORTUNITY_MECE_BASKET_LONG_NO
    )

    if not event.relationship_established:
        return _build_opportunity(
            opportunity_type=opp_type,
            guaranteed_payout_cents=0,
            total_cost_cents=0,
            payouts_by_outcome={o: 0 for o in event.outcomes},
            total_contracts=qty * len(event.outcomes),
            contract_ids=tuple(event.outcomes),
            fee_per_contract_cents=fee_cents,
            rejection_reason=(
                "Undeclared outcome relationship: event must have relationship_established=True "
                "(mutually exclusive and collectively exhaustive relationship is not established)"
            ),
        )

    missing_outcomes = [o for o in event.outcomes if o not in outcome_prices_cents]
    if missing_outcomes:
        return _build_opportunity(
            opportunity_type=opp_type,
            guaranteed_payout_cents=0,
            total_cost_cents=0,
            payouts_by_outcome={o: 0 for o in event.outcomes},
            total_contracts=qty * len(event.outcomes),
            contract_ids=tuple(event.outcomes),
            fee_per_contract_cents=fee_cents,
            rejection_reason=f"Incomplete basket: price missing for outcome(s): {missing_outcomes}",
        )

    for outcome, price in outcome_prices_cents.items():
        if outcome in event.outcomes:
            _require_int_cents(price, f"price for outcome {outcome!r}")

    contracts = tuple(
        BinaryContract(identifier=outcome, price_cents=outcome_prices_cents[outcome], yes_outcome=outcome)
        for outcome in event.outcomes
    )
    positions = tuple(
        Position(contract_id=outcome, side=basket_side, quantity=qty)
        for outcome in event.outcomes
    )
    portfolio = Portfolio(event=event, contracts=contracts, positions=positions)

    try:
        payouts_by_outcome = portfolio_payouts_cents(portfolio)
    except ContractInputError as exc:
        raise ArbitrageInputError(f"portfolio payout calculation failed: {exc}") from exc

    total_cost_cents = qty * sum(outcome_prices_cents[outcome] for outcome in event.outcomes)
    guaranteed_payout_cents = min(payouts_by_outcome.values()) if payouts_by_outcome else 0

    return _build_opportunity(
        opportunity_type=opp_type,
        guaranteed_payout_cents=guaranteed_payout_cents,
        total_cost_cents=total_cost_cents,
        payouts_by_outcome=payouts_by_outcome,
        total_contracts=qty * len(event.outcomes),
        contract_ids=tuple(event.outcomes),
        fee_per_contract_cents=fee_cents,
    )


def evaluate_portfolio(
    portfolio: Portfolio,
    entry_prices_cents: Mapping[str, int],
    fee_per_contract_cents: Optional[int] = None,
) -> ArbitrageOpportunity:
    """Evaluate arbitrage for an arbitrary Portfolio with explicitly established outcomes.

    Calculates exact payouts across every declared event outcome and compares
    the guaranteed worst-case payout against total position acquisition cost.
    """
    if not isinstance(portfolio, Portfolio):
        raise ArbitrageInputError("portfolio must be a Portfolio instance")
    if not isinstance(entry_prices_cents, Mapping):
        raise ArbitrageInputError("entry_prices_cents must be a mapping")
    fee_cents = _optional_fee_per_contract(fee_per_contract_cents)

    if not portfolio.positions:
        raise ArbitrageInputError("portfolio must contain at least one position")

    contract_ids = tuple(contract.identifier for contract in portfolio.contracts)

    if not portfolio.event.relationship_established:
        return _build_opportunity(
            opportunity_type=OPPORTUNITY_PORTFOLIO,
            guaranteed_payout_cents=0,
            total_cost_cents=0,
            payouts_by_outcome={o: 0 for o in portfolio.event.outcomes},
            total_contracts=sum(p.quantity for p in portfolio.positions),
            contract_ids=contract_ids,
            fee_per_contract_cents=fee_cents,
            rejection_reason=(
                "Undeclared outcome relationship: event must have relationship_established=True "
                "(mutually exclusive and collectively exhaustive relationship is not established)"
            ),
        )

    for pos in portfolio.positions:
        if pos.contract_id not in entry_prices_cents:
            raise ArbitrageInputError(
                f"missing entry price for contract {pos.contract_id!r}"
            )
        _require_int_cents(entry_prices_cents[pos.contract_id], f"entry_price for {pos.contract_id!r}")

    total_cost_cents = sum(
        pos.quantity * entry_prices_cents[pos.contract_id]
        for pos in portfolio.positions
    )

    try:
        payouts_by_outcome = portfolio_payouts_cents(portfolio)
    except ContractInputError as exc:
        raise ArbitrageInputError(f"portfolio payout calculation failed: {exc}") from exc

    guaranteed_payout_cents = min(payouts_by_outcome.values()) if payouts_by_outcome else 0
    total_contracts = sum(pos.quantity for pos in portfolio.positions)

    return _build_opportunity(
        opportunity_type=OPPORTUNITY_PORTFOLIO,
        guaranteed_payout_cents=guaranteed_payout_cents,
        total_cost_cents=total_cost_cents,
        payouts_by_outcome=payouts_by_outcome,
        total_contracts=total_contracts,
        contract_ids=contract_ids,
        fee_per_contract_cents=fee_cents,
    )


def evaluate_mece_markets(
    event: Event,
    markets: Sequence[NormalizedMarket],
    market_outcome_map: Mapping[str, str],
    basket_side: str = YES,
    quantity: int = 1,
    fee_per_contract_cents: Optional[int] = None,
) -> ArbitrageOpportunity:
    """Evaluate an event basket using NormalizedMarket objects and an explicit outcome mapping.

    market_outcome_map explicitly maps market.ticker -> outcome_name in event.outcomes.
    This guarantees that relationships between markets and event outcomes are proven and
    provided by the caller, never inferred from market titles or tickers.
    """
    if not isinstance(event, Event):
        raise ArbitrageInputError("event must be an Event instance")
    if not isinstance(markets, Sequence) or isinstance(markets, (str, bytes)):
        raise ArbitrageInputError("markets must be a sequence of NormalizedMarket instances")
    if not isinstance(market_outcome_map, Mapping):
        raise ArbitrageInputError("market_outcome_map must be a mapping of market_ticker -> outcome")
    if basket_side not in (YES, NO):
        raise ArbitrageInputError(f"basket_side must be {YES!r} or {NO!r}")

    markets_by_ticker = {}
    for m in markets:
        if not isinstance(m, NormalizedMarket):
            raise ArbitrageInputError("all items in markets must be NormalizedMarket instances")
        markets_by_ticker[m.ticker] = m

    outcome_prices_cents: Dict[str, int] = {}
    for ticker, outcome in market_outcome_map.items():
        if ticker not in markets_by_ticker:
            raise ArbitrageInputError(f"mapped ticker {ticker!r} not found in provided markets")
        if outcome not in event.outcomes:
            raise ArbitrageInputError(f"mapped outcome {outcome!r} not in event.outcomes")

        market = markets_by_ticker[ticker]
        ask_dollars = market.yes_ask_dollars if basket_side == YES else market.no_ask_dollars
        if ask_dollars is None:
            side_str = "yes_ask_dollars" if basket_side == YES else "no_ask_dollars"
            opp_type = (
                OPPORTUNITY_MECE_BASKET_LONG_YES
                if basket_side == YES
                else OPPORTUNITY_MECE_BASKET_LONG_NO
            )
            return _build_opportunity(
                opportunity_type=opp_type,
                guaranteed_payout_cents=0,
                total_cost_cents=0,
                payouts_by_outcome={o: 0 for o in event.outcomes},
                total_contracts=quantity * len(event.outcomes) if isinstance(quantity, int) and quantity > 0 else 0,
                contract_ids=tuple(event.outcomes),
                fee_per_contract_cents=fee_per_contract_cents,
                rejection_reason=f"Incomplete market quotes: market {ticker!r} is missing {side_str}",
            )
        outcome_prices_cents[outcome] = dollars_to_cents(ask_dollars)

    return evaluate_mece_event_basket(
        event=event,
        outcome_prices_cents=outcome_prices_cents,
        basket_side=basket_side,
        quantity=quantity,
        fee_per_contract_cents=fee_per_contract_cents,
    )
