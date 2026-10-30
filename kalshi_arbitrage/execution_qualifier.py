"""Deterministic execution-aware arbitrage qualification engine for Kalshi prediction markets.

V22 Execution-Aware Arbitrage Qualification:
Evaluates detected arbitrage candidates against realistic limit order-book depth,
multi-leg liquidity bottlenecks, price slippage (VWAP), official CFTC quadratic taker
fees, multi-leg timestamp synchronization, and data freshness invariants.

Governing Principles:
1. Strict Qualification Outcomes: Every candidate receives an explicit, auditable
   qualification outcome (QUALIFIED, REJECTED_INSUFFICIENT_DEPTH, REJECTED_UNPROFITABLE_FEES,
   REJECTED_NO_GROSS_EDGE, REJECTED_INVALID_BASKET, REJECTED_STALE, REJECTED_LEG_SKEW,
   REJECTED_MISSING_MARKET, REJECTED_MISSING_TIMESTAMP, INCONCLUSIVE).
2. Multi-Level Depth & Bottlenecks: In Kalshi binary markets, available asks derive
   from resting complementary bids. Evaluates multi-level consumption and identifies
   the common executable quantity supported across all required legs.
3. Accurate Financial Arithmetic: Integer cents for all costs, payouts, fees, and edges.
   Exact Decimal representation for unit metrics and percentage return on capital.
4. Independent Leg Fees: Official CFTC quadratic taker fees evaluated per leg
   across all consumed price tiers.
5. Invalidation on Stale or Skewed Data: Stale observations (> max_stale_seconds) or
   excessive multi-leg timestamp skew (> max_leg_skew_seconds) fail closed.
6. Honest Disclaimers: Static order-book qualification does not guarantee live execution,
   atomic multi-leg fills, or realized profit.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import logging
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)
import uuid

from .arbitrage import (
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
    cents_to_dollars,
)
from .contract import NO, YES
from .execution import (
    AskLevel,
    ConsumedLevel,
    DepthTraversalResult,
    derive_ask_levels,
    traverse_order_book_depth,
)
from .fee_model import (
    CalculatedFee,
    FeeModelConfig,
    FeeScheduleType,
    FillTier,
    calculate_multi_level_fee,
    calculate_single_level_fee,
)
from .market_data import NormalizedMarket, NormalizedOrderBook
from .observer import DeclaredMeceBasket, MarketObservation
from .portfolio import Event
from .snapshot_reconstruction import _effective_timestamp

logger = logging.getLogger("execution_qualifier")

_EXECUTION_QUALIFICATION_DISCLAIMERS: Tuple[str, ...] = (
    "Static snapshot assumption: depth and fees evaluated against resting limit orders observed at snapshot time; actual live execution subject to queue priority and network latency.",
    "Non-atomic multi-leg execution risk: orders on separate market legs execute independently; slippage or partial fill on one leg creates unhedged residual risk.",
    "Transient liquidity risk: resting counterparty limit orders may be cancelled or matched by competing orders prior to arrival.",
    "Exchange fee schedule: taker fees calculated using official CFTC-regulated quadratic formula.",
)


class QualificationOutcome(str, Enum):
    """Explicit qualification outcomes for an arbitrage candidate."""

    QUALIFIED = "QUALIFIED"
    REJECTED_INSUFFICIENT_DEPTH = "REJECTED_INSUFFICIENT_DEPTH"
    REJECTED_UNPROFITABLE_FEES = "REJECTED_UNPROFITABLE_FEES"
    REJECTED_NO_GROSS_EDGE = "REJECTED_NO_GROSS_EDGE"
    REJECTED_INVALID_BASKET = "REJECTED_INVALID_BASKET"
    REJECTED_STALE = "REJECTED_STALE"
    REJECTED_LEG_SKEW = "REJECTED_LEG_SKEW"
    REJECTED_MISSING_MARKET = "REJECTED_MISSING_MARKET"
    REJECTED_MISSING_TIMESTAMP = "REJECTED_MISSING_TIMESTAMP"
    REJECTED_BASKET_TOO_LARGE = "REJECTED_BASKET_TOO_LARGE"
    REJECTED_EVENT_PREFILTER = "REJECTED_EVENT_PREFILTER"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class LegExecutionDetail:
    """Auditable execution details for an individual leg of an arbitrage candidate."""

    market_ticker: str
    side: str
    requested_quantity: int
    executable_quantity: int
    available_depth_contracts: int
    top_of_book_price_cents: Optional[int]
    average_price_cents: Optional[Decimal]
    total_cost_cents: int
    fee_cents: int
    consumed_levels: Tuple[Dict[str, Any], ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market_ticker": self.market_ticker,
            "side": self.side,
            "requested_quantity": self.requested_quantity,
            "executable_quantity": self.executable_quantity,
            "available_depth_contracts": self.available_depth_contracts,
            "top_of_book_price_cents": self.top_of_book_price_cents,
            "average_price_cents": (
                str(self.average_price_cents) if self.average_price_cents is not None else None
            ),
            "total_cost_cents": self.total_cost_cents,
            "fee_cents": self.fee_cents,
            "consumed_levels": list(self.consumed_levels),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LegExecutionDetail":
        avg = Decimal(str(d["average_price_cents"])) if d.get("average_price_cents") is not None else None
        return cls(
            market_ticker=str(d["market_ticker"]),
            side=str(d["side"]),
            requested_quantity=int(d["requested_quantity"]),
            executable_quantity=int(d["executable_quantity"]),
            available_depth_contracts=int(d["available_depth_contracts"]),
            top_of_book_price_cents=d.get("top_of_book_price_cents"),
            average_price_cents=avg,
            total_cost_cents=int(d["total_cost_cents"]),
            fee_cents=int(d["fee_cents"]),
            consumed_levels=tuple(d.get("consumed_levels", ())),
        )


@dataclass(frozen=True)
class CandidateQualification:
    """Comprehensive, auditable execution qualification record for an arbitrage candidate."""

    qualification_id: str
    candidate_id: str
    strategy_type: str
    evaluated_at: str
    outcome: QualificationOutcome
    is_qualified: bool
    rejection_reason: Optional[str]
    requested_quantity: int
    executable_quantity: int
    is_partial_fill: bool
    gross_edge_cents: int
    gross_edge_per_unit_cents: Decimal
    total_cost_cents: int
    guaranteed_payout_cents: int
    total_fees_cents: int
    fees_per_leg_cents: Dict[str, int]
    net_edge_cents: int
    net_edge_per_unit_cents: Decimal
    return_on_capital_pct: Optional[Decimal]
    leg_details: Tuple[LegExecutionDetail, ...]
    max_observed_skew_seconds: float
    data_freshness_seconds: float
    assumptions_and_limitations: Tuple[str, ...] = _EXECUTION_QUALIFICATION_DISCLAIMERS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "qualification_id": self.qualification_id,
            "candidate_id": self.candidate_id,
            "strategy_type": self.strategy_type,
            "evaluated_at": self.evaluated_at,
            "outcome": self.outcome.value,
            "is_qualified": self.is_qualified,
            "rejection_reason": self.rejection_reason,
            "requested_quantity": self.requested_quantity,
            "executable_quantity": self.executable_quantity,
            "is_partial_fill": self.is_partial_fill,
            "gross_edge_cents": self.gross_edge_cents,
            "gross_edge_per_unit_cents": str(self.gross_edge_per_unit_cents),
            "total_cost_cents": self.total_cost_cents,
            "guaranteed_payout_cents": self.guaranteed_payout_cents,
            "total_fees_cents": self.total_fees_cents,
            "fees_per_leg_cents": dict(self.fees_per_leg_cents),
            "net_edge_cents": self.net_edge_cents,
            "net_edge_per_unit_cents": str(self.net_edge_per_unit_cents),
            "return_on_capital_pct": (
                str(self.return_on_capital_pct) if self.return_on_capital_pct is not None else None
            ),
            "leg_details": [l.to_dict() for l in self.leg_details],
            "max_observed_skew_seconds": self.max_observed_skew_seconds,
            "data_freshness_seconds": self.data_freshness_seconds,
            "assumptions_and_limitations": list(self.assumptions_and_limitations),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CandidateQualification":
        outcome_val = d["outcome"]
        outcome = (
            QualificationOutcome(outcome_val)
            if outcome_val in QualificationOutcome._value2member_map_
            else QualificationOutcome.INCONCLUSIVE
        )
        roc = Decimal(str(d["return_on_capital_pct"])) if d.get("return_on_capital_pct") is not None else None
        return cls(
            qualification_id=str(d["qualification_id"]),
            candidate_id=str(d["candidate_id"]),
            strategy_type=str(d["strategy_type"]),
            evaluated_at=str(d["evaluated_at"]),
            outcome=outcome,
            is_qualified=bool(d["is_qualified"]),
            rejection_reason=d.get("rejection_reason"),
            requested_quantity=int(d["requested_quantity"]),
            executable_quantity=int(d["executable_quantity"]),
            is_partial_fill=bool(d["is_partial_fill"]),
            gross_edge_cents=int(d["gross_edge_cents"]),
            gross_edge_per_unit_cents=Decimal(str(d["gross_edge_per_unit_cents"])),
            total_cost_cents=int(d["total_cost_cents"]),
            guaranteed_payout_cents=int(d["guaranteed_payout_cents"]),
            total_fees_cents=int(d["total_fees_cents"]),
            fees_per_leg_cents={k: int(v) for k, v in d.get("fees_per_leg_cents", {}).items()},
            net_edge_cents=int(d["net_edge_cents"]),
            net_edge_per_unit_cents=Decimal(str(d["net_edge_per_unit_cents"])),
            return_on_capital_pct=roc,
            leg_details=tuple(LegExecutionDetail.from_dict(item) for item in d.get("leg_details", ())),
            max_observed_skew_seconds=float(d.get("max_observed_skew_seconds", 0.0)),
            data_freshness_seconds=float(d.get("data_freshness_seconds", 0.0)),
            assumptions_and_limitations=tuple(d.get("assumptions_and_limitations", _EXECUTION_QUALIFICATION_DISCLAIMERS)),
        )


@dataclass(frozen=True)
class ExecutionQualifierConfig:
    """Configuration options for execution-aware arbitrage qualification."""

    target_quantity: int = 1
    max_stale_seconds: float = 60.0
    max_leg_skew_seconds: float = 5.0
    fee_model_config: FeeModelConfig = field(
        default_factory=lambda: FeeModelConfig(schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER)
    )
    require_full_quantity: bool = True
    min_net_profit_cents: int = 1
    require_source_timestamp: bool = True

    def __post_init__(self) -> None:
        if self.target_quantity <= 0:
            raise ValueError("target_quantity must be a positive integer")
        if self.max_stale_seconds <= 0:
            raise ValueError("max_stale_seconds must be positive")
        if self.max_leg_skew_seconds <= 0:
            raise ValueError("max_leg_skew_seconds must be positive")
        if self.min_net_profit_cents < 0:
            raise ValueError("min_net_profit_cents cannot be negative")


class ExecutionAwareArbitrageQualifier:
    """Deterministic qualification engine evaluating candidates against execution constraints."""

    def __init__(self, config: Optional[ExecutionQualifierConfig] = None) -> None:
        self.config = config or ExecutionQualifierConfig()

    def qualify_binary_parity(
        self,
        observation: MarketObservation,
        requested_quantity: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> CandidateQualification:
        """Evaluate execution feasibility and net profit for a single-market binary parity candidate."""
        t_eval = now or datetime.now(timezone.utc)
        req_qty = requested_quantity or self.config.target_quantity
        cand_id = f"BP:{observation.ticker}"
        qual_id = f"qual-{uuid.uuid4().hex[:10]}"

        # 1. Validate data presence and freshness
        if observation is None or not observation.ticker:
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=OPPORTUNITY_BINARY_PARITY,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_MISSING_MARKET,
                reason="Null or missing observation payload",
                req_qty=req_qty,
            )

        if not observation.is_success or observation.order_book is None:
            reason = observation.error_message or "Order book missing or market fetch failed"
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=OPPORTUNITY_BINARY_PARITY,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_MISSING_MARKET,
                reason=reason,
                req_qty=req_qty,
            )

        # Freshness check
        obs_time = observation.observed_at or observation.local_receive_time
        freshness_s = max(0.0, (t_eval - obs_time).total_seconds()) if obs_time else 0.0
        if observation.is_stale or freshness_s > self.config.max_stale_seconds:
            reason = observation.staleness_reason or f"Observation age {freshness_s:.1f}s exceeds limit {self.config.max_stale_seconds}s"
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=OPPORTUNITY_BINARY_PARITY,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_STALE,
                reason=reason,
                req_qty=req_qty,
                data_freshness_seconds=freshness_s,
            )

        book = observation.order_book
        if self.config.require_source_timestamp and book.source_timestamp is None:
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=OPPORTUNITY_BINARY_PARITY,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_MISSING_TIMESTAMP,
                reason="Order book is missing exchange source_timestamp header",
                req_qty=req_qty,
                data_freshness_seconds=freshness_s,
            )

        # 2. Derive available asks from resting complementary bids
        yes_asks = derive_ask_levels(book, YES)
        no_asks = derive_ask_levels(book, NO)

        available_yes_depth = sum(lvl.quantity for lvl in yes_asks)
        available_no_depth = sum(lvl.quantity for lvl in no_asks)

        top_yes_ask = yes_asks[0].price_cents if yes_asks else None
        top_no_ask = no_asks[0].price_cents if no_asks else None

        # Check for empty depth
        if available_yes_depth == 0 or available_no_depth == 0:
            missing_sides = []
            if available_yes_depth == 0:
                missing_sides.append(f"{YES} (no resting NO bids)")
            if available_no_depth == 0:
                missing_sides.append(f"{NO} (no resting YES bids)")
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=OPPORTUNITY_BINARY_PARITY,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_INSUFFICIENT_DEPTH,
                reason=f"Zero executable depth on side(s): {', '.join(missing_sides)}",
                req_qty=req_qty,
                data_freshness_seconds=freshness_s,
            )

        # 3. Initial depth traversal across both legs
        trav_yes = traverse_order_book_depth(book, YES, req_qty)
        trav_no = traverse_order_book_depth(book, NO, req_qty)

        common_qty = min(trav_yes.supported_quantity, trav_no.supported_quantity)

        if common_qty == 0:
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=OPPORTUNITY_BINARY_PARITY,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_INSUFFICIENT_DEPTH,
                reason="Common supported executable quantity across YES and NO legs is 0",
                req_qty=req_qty,
                data_freshness_seconds=freshness_s,
            )

        if self.config.require_full_quantity and common_qty < req_qty:
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=OPPORTUNITY_BINARY_PARITY,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_INSUFFICIENT_DEPTH,
                reason=f"Insufficient depth for full requested quantity: requested {req_qty}, only {common_qty} supported",
                req_qty=req_qty,
                exec_qty=common_qty,
                data_freshness_seconds=freshness_s,
            )

        # Re-traverse at exact common quantity if partial
        if common_qty < req_qty:
            trav_yes = traverse_order_book_depth(book, YES, common_qty)
            trav_no = traverse_order_book_depth(book, NO, common_qty)

        # 4. Multi-level pricing, payouts, and fees
        cost_yes = trav_yes.total_cost_cents
        cost_no = trav_no.total_cost_cents
        total_cost_cents = cost_yes + cost_no
        guaranteed_payout_cents = common_qty * 100
        gross_edge_cents = guaranteed_payout_cents - total_cost_cents

        series_ticker = (
            observation.market.series_ticker
            if (observation.market and hasattr(observation.market, "series_ticker"))
            else None
        )

        # Calculate exact official fees independently per leg
        tiers_yes = [FillTier(price_cents=lvl.price_cents, quantity=lvl.quantity) for lvl in trav_yes.consumed_levels]
        tiers_no = [FillTier(price_cents=lvl.price_cents, quantity=lvl.quantity) for lvl in trav_no.consumed_levels]

        fee_res_yes = calculate_multi_level_fee(tiers_yes, config=self.config.fee_model_config, series_ticker=series_ticker)
        fee_res_no = calculate_multi_level_fee(tiers_no, config=self.config.fee_model_config, series_ticker=series_ticker)

        fee_yes = fee_res_yes.total_fee_cents
        fee_no = fee_res_no.total_fee_cents
        total_fees_cents = fee_yes + fee_no

        net_edge_cents = gross_edge_cents - total_fees_cents

        gross_edge_per_unit = (Decimal(gross_edge_cents) / Decimal(common_qty)).quantize(Decimal("0.01"))
        net_edge_per_unit = (Decimal(net_edge_cents) / Decimal(common_qty)).quantize(Decimal("0.01"))

        roc_pct: Optional[Decimal] = None
        if total_cost_cents > 0:
            roc_pct = ((Decimal(net_edge_cents) / Decimal(total_cost_cents)) * Decimal("100")).quantize(Decimal("0.01"))

        # Build leg details
        leg_yes_detail = LegExecutionDetail(
            market_ticker=observation.ticker,
            side=YES,
            requested_quantity=req_qty,
            executable_quantity=common_qty,
            available_depth_contracts=available_yes_depth,
            top_of_book_price_cents=top_yes_ask,
            average_price_cents=trav_yes.average_price_cents,
            total_cost_cents=cost_yes,
            fee_cents=fee_yes,
            consumed_levels=tuple(
                {
                    "price_cents": lvl.price_cents,
                    "price_dollars": str(lvl.price_dollars),
                    "quantity": lvl.quantity,
                    "cost_cents": lvl.cost_cents,
                }
                for lvl in trav_yes.consumed_levels
            ),
        )

        leg_no_detail = LegExecutionDetail(
            market_ticker=observation.ticker,
            side=NO,
            requested_quantity=req_qty,
            executable_quantity=common_qty,
            available_depth_contracts=available_no_depth,
            top_of_book_price_cents=top_no_ask,
            average_price_cents=trav_no.average_price_cents,
            total_cost_cents=cost_no,
            fee_cents=fee_no,
            consumed_levels=tuple(
                {
                    "price_cents": lvl.price_cents,
                    "price_dollars": str(lvl.price_dollars),
                    "quantity": lvl.quantity,
                    "cost_cents": lvl.cost_cents,
                }
                for lvl in trav_no.consumed_levels
            ),
        )

        # 5. Qualification Decision Logic
        if gross_edge_cents <= 0:
            outcome = QualificationOutcome.REJECTED_NO_GROSS_EDGE
            reason = (
                f"No gross edge: total cost ({total_cost_cents}¢ for {common_qty} pairs) "
                f">= guaranteed payout ({guaranteed_payout_cents}¢)"
            )
            is_qual = False
        elif net_edge_cents < self.config.min_net_profit_cents:
            outcome = QualificationOutcome.REJECTED_UNPROFITABLE_FEES
            reason = (
                f"Unprofitable after fees: gross edge {gross_edge_cents}¢ - fees {total_fees_cents}¢ "
                f"= net edge {net_edge_cents}¢ < {self.config.min_net_profit_cents}¢"
            )
            is_qual = False
        else:
            outcome = QualificationOutcome.QUALIFIED
            reason = None
            is_qual = True

        return CandidateQualification(
            qualification_id=qual_id,
            candidate_id=cand_id,
            strategy_type=OPPORTUNITY_BINARY_PARITY,
            evaluated_at=t_eval.isoformat(),
            outcome=outcome,
            is_qualified=is_qual,
            rejection_reason=reason,
            requested_quantity=req_qty,
            executable_quantity=common_qty,
            is_partial_fill=(common_qty < req_qty),
            gross_edge_cents=gross_edge_cents,
            gross_edge_per_unit_cents=gross_edge_per_unit,
            total_cost_cents=total_cost_cents,
            guaranteed_payout_cents=guaranteed_payout_cents,
            total_fees_cents=total_fees_cents,
            fees_per_leg_cents={
                f"{observation.ticker}:{YES}": fee_yes,
                f"{observation.ticker}:{NO}": fee_no,
            },
            net_edge_cents=net_edge_cents,
            net_edge_per_unit_cents=net_edge_per_unit,
            return_on_capital_pct=roc_pct,
            leg_details=(leg_yes_detail, leg_no_detail),
            max_observed_skew_seconds=0.0,
            data_freshness_seconds=freshness_s,
        )

    def qualify_mece_basket(
        self,
        basket: DeclaredMeceBasket,
        observations: Mapping[str, MarketObservation],
        requested_quantity: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> CandidateQualification:
        """Evaluate execution feasibility, multi-leg bottleneck depth, and net profit for a declared MECE basket."""
        t_eval = now or datetime.now(timezone.utc)
        req_qty = requested_quantity or self.config.target_quantity
        cand_id = f"MECE:{basket.event_ticker}:{basket.basket_side}"
        qual_id = f"qual-{uuid.uuid4().hex[:10]}"
        opp_type = (
            OPPORTUNITY_MECE_BASKET_LONG_YES
            if basket.basket_side == YES
            else OPPORTUNITY_MECE_BASKET_LONG_NO
        )

        # 1. Validate basket declaration
        if not basket or not basket.event_ticker or not basket.outcomes:
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=opp_type,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_INVALID_BASKET,
                reason="Basket configuration is missing event_ticker or declared outcomes",
                req_qty=req_qty,
            )

        # 2. Check for missing constituent legs
        missing_tickers = [t for t in basket.ordered_tickers if t not in observations]
        if missing_tickers:
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=opp_type,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_MISSING_MARKET,
                reason=f"Constituent market observation(s) missing from dataset: {missing_tickers}",
                req_qty=req_qty,
            )

        constituent_obs = [observations[t] for t in basket.ordered_tickers]

        # 3. Check for transport failures or empty order books
        failed_legs = [o.ticker for o in constituent_obs if not o.is_success or o.order_book is None]
        if failed_legs:
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=opp_type,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_MISSING_MARKET,
                reason=f"Failed observation or null order book on leg(s): {failed_legs}",
                req_qty=req_qty,
            )

        # 4. Check staleness and timestamp skew across all constituent legs
        stale_legs = []
        timestamps: List[datetime] = []
        missing_src_ts = []
        max_freshness_s = 0.0

        for o in constituent_obs:
            obs_time = o.observed_at or o.local_receive_time
            if obs_time:
                age_s = max(0.0, (t_eval - obs_time).total_seconds())
                if age_s > max_freshness_s:
                    max_freshness_s = age_s
                if o.is_stale or age_s > self.config.max_stale_seconds:
                    stale_legs.append(o.ticker)

            src_ts = o.source_timestamp or (o.order_book.source_timestamp if o.order_book else None)
            if src_ts is not None:
                eff_ts = _effective_timestamp(o)
                if eff_ts:
                    timestamps.append(eff_ts)
            else:
                missing_src_ts.append(o.ticker)

        if stale_legs:
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=opp_type,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_STALE,
                reason=f"Constituent market leg(s) exceed staleness limit: {stale_legs}",
                req_qty=req_qty,
                data_freshness_seconds=max_freshness_s,
            )

        if self.config.require_source_timestamp and missing_src_ts:
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=opp_type,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_MISSING_TIMESTAMP,
                reason=f"Legs missing exchange source_timestamp header: {missing_src_ts}",
                req_qty=req_qty,
                data_freshness_seconds=max_freshness_s,
            )

        skew_s = 0.0
        if len(timestamps) >= 2:
            min_ts = min(timestamps)
            max_ts = max(timestamps)
            skew_s = (max_ts - min_ts).total_seconds()
            if skew_s > self.config.max_leg_skew_seconds:
                return self._build_rejection(
                    qual_id=qual_id,
                    cand_id=cand_id,
                    strategy_type=opp_type,
                    t_eval=t_eval,
                    outcome=QualificationOutcome.REJECTED_LEG_SKEW,
                    reason=f"Cross-leg timestamp skew {skew_s:.2f}s exceeds maximum allowable threshold {self.config.max_leg_skew_seconds:.2f}s",
                    req_qty=req_qty,
                    max_skew=skew_s,
                    data_freshness_seconds=max_freshness_s,
                )

        # 5. Multi-leg order-book depth traversal
        initial_traversals: List[DepthTraversalResult] = []
        available_depths: Dict[str, int] = {}
        top_asks: Dict[str, Optional[int]] = {}

        for t in basket.ordered_tickers:
            obs = observations[t]
            book = obs.order_book
            asks = derive_ask_levels(book, basket.basket_side)
            depth_contracts = sum(lvl.quantity for lvl in asks)
            available_depths[t] = depth_contracts
            top_asks[t] = asks[0].price_cents if asks else None

            trav = traverse_order_book_depth(book, basket.basket_side, req_qty)
            initial_traversals.append(trav)

        # Identify bottleneck quantity supported across all legs
        common_qty = min(t.supported_quantity for t in initial_traversals)

        if common_qty == 0:
            unfunded_legs = [t.market_ticker for t in initial_traversals if t.supported_quantity == 0]
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=opp_type,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_INSUFFICIENT_DEPTH,
                reason=f"Zero executable depth available on bottleneck leg(s): {unfunded_legs}",
                req_qty=req_qty,
                max_skew=skew_s,
                data_freshness_seconds=max_freshness_s,
            )

        if self.config.require_full_quantity and common_qty < req_qty:
            return self._build_rejection(
                qual_id=qual_id,
                cand_id=cand_id,
                strategy_type=opp_type,
                t_eval=t_eval,
                outcome=QualificationOutcome.REJECTED_INSUFFICIENT_DEPTH,
                reason=f"Insufficient depth for full requested quantity: requested {req_qty}, only {common_qty} supported across all legs",
                req_qty=req_qty,
                exec_qty=common_qty,
                max_skew=skew_s,
                data_freshness_seconds=max_freshness_s,
            )

        # Re-traverse at exact common quantity if partial
        final_traversals = (
            initial_traversals
            if common_qty == req_qty
            else [
                traverse_order_book_depth(observations[t].order_book, basket.basket_side, common_qty)
                for t in basket.ordered_tickers
            ]
        )

        # 6. Pricing, payouts, and per-leg fees
        total_cost_cents = sum(t.total_cost_cents for t in final_traversals)
        num_outcomes = len(basket.outcomes)

        if basket.basket_side == YES:
            guaranteed_payout_cents = common_qty * 100
        else:
            guaranteed_payout_cents = common_qty * (num_outcomes - 1) * 100

        gross_edge_cents = guaranteed_payout_cents - total_cost_cents

        fees_per_leg: Dict[str, int] = {}
        total_fees_cents = 0
        leg_details_list: List[LegExecutionDetail] = []

        for trav in final_traversals:
            t_ticker = trav.market_ticker
            tiers = [FillTier(price_cents=lvl.price_cents, quantity=lvl.quantity) for lvl in trav.consumed_levels]
            leg_fee_res = calculate_multi_level_fee(
                tiers, config=self.config.fee_model_config, series_ticker=basket.event_ticker
            )
            fee_c = leg_fee_res.total_fee_cents
            fees_per_leg[t_ticker] = fee_c
            total_fees_cents += fee_c

            detail = LegExecutionDetail(
                market_ticker=t_ticker,
                side=basket.basket_side,
                requested_quantity=req_qty,
                executable_quantity=common_qty,
                available_depth_contracts=available_depths.get(t_ticker, 0),
                top_of_book_price_cents=top_asks.get(t_ticker),
                average_price_cents=trav.average_price_cents,
                total_cost_cents=trav.total_cost_cents,
                fee_cents=fee_c,
                consumed_levels=tuple(
                    {
                        "price_cents": lvl.price_cents,
                        "price_dollars": str(lvl.price_dollars),
                        "quantity": lvl.quantity,
                        "cost_cents": lvl.cost_cents,
                    }
                    for lvl in trav.consumed_levels
                ),
            )
            leg_details_list.append(detail)

        net_edge_cents = gross_edge_cents - total_fees_cents

        gross_edge_per_unit = (Decimal(gross_edge_cents) / Decimal(common_qty)).quantize(Decimal("0.01"))
        net_edge_per_unit = (Decimal(net_edge_cents) / Decimal(common_qty)).quantize(Decimal("0.01"))

        roc_pct: Optional[Decimal] = None
        if total_cost_cents > 0:
            roc_pct = ((Decimal(net_edge_cents) / Decimal(total_cost_cents)) * Decimal("100")).quantize(Decimal("0.01"))

        # 7. Qualification Decision Logic
        if gross_edge_cents <= 0:
            outcome = QualificationOutcome.REJECTED_NO_GROSS_EDGE
            reason = (
                f"No gross edge: total acquisition cost ({total_cost_cents}¢ for {common_qty} units) "
                f">= guaranteed payout ({guaranteed_payout_cents}¢)"
            )
            is_qual = False
        elif net_edge_cents < self.config.min_net_profit_cents:
            outcome = QualificationOutcome.REJECTED_UNPROFITABLE_FEES
            reason = (
                f"Unprofitable after fees: gross edge {gross_edge_cents}¢ - fees {total_fees_cents}¢ "
                f"= net edge {net_edge_cents}¢ < {self.config.min_net_profit_cents}¢"
            )
            is_qual = False
        else:
            outcome = QualificationOutcome.QUALIFIED
            reason = None
            is_qual = True

        return CandidateQualification(
            qualification_id=qual_id,
            candidate_id=cand_id,
            strategy_type=opp_type,
            evaluated_at=t_eval.isoformat(),
            outcome=outcome,
            is_qualified=is_qual,
            rejection_reason=reason,
            requested_quantity=req_qty,
            executable_quantity=common_qty,
            is_partial_fill=(common_qty < req_qty),
            gross_edge_cents=gross_edge_cents,
            gross_edge_per_unit_cents=gross_edge_per_unit,
            total_cost_cents=total_cost_cents,
            guaranteed_payout_cents=guaranteed_payout_cents,
            total_fees_cents=total_fees_cents,
            fees_per_leg_cents=fees_per_leg,
            net_edge_cents=net_edge_cents,
            net_edge_per_unit_cents=net_edge_per_unit,
            return_on_capital_pct=roc_pct,
            leg_details=tuple(leg_details_list),
            max_observed_skew_seconds=skew_s,
            data_freshness_seconds=max_freshness_s,
        )

    def _build_rejection(
        self,
        qual_id: str,
        cand_id: str,
        strategy_type: str,
        t_eval: datetime,
        outcome: QualificationOutcome,
        reason: str,
        req_qty: int,
        exec_qty: int = 0,
        max_skew: float = 0.0,
        data_freshness_seconds: float = 0.0,
    ) -> CandidateQualification:
        """Helper to construct a standardized rejected qualification record."""
        return CandidateQualification(
            qualification_id=qual_id,
            candidate_id=cand_id,
            strategy_type=strategy_type,
            evaluated_at=t_eval.isoformat(),
            outcome=outcome,
            is_qualified=False,
            rejection_reason=reason,
            requested_quantity=req_qty,
            executable_quantity=exec_qty,
            is_partial_fill=False,
            gross_edge_cents=0,
            gross_edge_per_unit_cents=Decimal("0.00"),
            total_cost_cents=0,
            guaranteed_payout_cents=0,
            total_fees_cents=0,
            fees_per_leg_cents={},
            net_edge_cents=0,
            net_edge_per_unit_cents=Decimal("0.00"),
            return_on_capital_pct=None,
            leg_details=(),
            max_observed_skew_seconds=max_skew,
            data_freshness_seconds=data_freshness_seconds,
        )
