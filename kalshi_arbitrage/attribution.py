"""Kalshi Arbitrage Engine V18 — Opportunity Economics & Attribution Analyzer.

Provides deterministic mathematical attribution explaining why arbitrage opportunities
exist or fail across both sides (Long YES and Long NO) of explicitly validated MECE event baskets.

Governing Principles:
1. Exact Payout Semantics:
   - For an N-outcome MECE partition:
     - Long YES basket: 1 YES contract on each outcome -> guaranteed payout = 100¢ ($1.00).
     - Long NO basket: 1 NO contract on each outcome -> guaranteed payout = (N - 1) * 100¢.
2. Two-Sided Microstructure Invariant:
   - In any binary limit order book:
     Gross Edge (YES) = 100¢ - Sum(Ask_YES)
     Gross Edge (NO) = Sum(Bid_YES) - 100¢
     Gross Edge (YES) + Gross Edge (NO) = -Sum(Ask_YES - Bid_YES) = -Sum(Spread) <= 0
   - Both sides cannot be simultaneously profitable at resting quotes unless there is an inverted book.
3. Causal Failure Taxonomy:
   - NEGATIVE_GROSS_EDGE: Sum of asks exceeds guaranteed payout before fees.
   - FEE_EROSION: Positive gross edge is fully consumed by CFTC taker fees.
   - INSUFFICIENT_DEPTH: Positive net edge exists, but available book depth cannot support desired quantity.
   - DATA_QUALITY_OR_SKEW: Excessive cross-leg timing skew, missing timestamps, or stale quotes.
   - UNVERIFIED_RELATIONSHIP: Settlement outcomes are not proven to be mutually exclusive and collectively exhaustive.
   - QUALIFIED_ARBITRAGE: Fully qualified, executable, fee-adjusted positive net edge.
4. Determinism & Integrity:
   - Reuses existing execution and fee calculation engines.
   - Uses exact integer cents for financial calculations.
"""

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .arbitrage import (
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
    dollars_to_cents,
)
from .contract import NO, YES
from .execution import (
    DepthTraversalResult,
    ExecutionPricingResult,
    derive_ask_levels,
    evaluate_mece_basket_execution,
)
from .fee_model import FeeModelConfig, FeeScheduleType
from .market_data import NormalizedOrderBook
from .portfolio import Event
from .snapshot_reconstruction import (
    ReconstructedBasketSnapshot,
    STATUS_VALID_SNAPSHOT,
)


class EconomicAttributionCategory(str, Enum):
    """Exhaustive causal categories for opportunity evaluation outcomes."""

    NEGATIVE_GROSS_EDGE = "NEGATIVE_GROSS_EDGE"
    FEE_EROSION = "FEE_EROSION"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
    DATA_QUALITY_OR_SKEW = "DATA_QUALITY_OR_SKEW"
    UNVERIFIED_RELATIONSHIP = "UNVERIFIED_RELATIONSHIP"
    QUALIFIED_ARBITRAGE = "QUALIFIED_ARBITRAGE"


@dataclass(frozen=True)
class BasketSideAttribution:
    """Attribution analysis for a single side (YES or NO) of a basket candidate."""

    side: str
    guaranteed_payout_cents: int
    acquisition_cost_cents: int
    gross_edge_cents: int
    is_gross_profitable: bool
    estimated_fees_cents: Optional[int]
    net_edge_cents: Optional[int]
    is_net_profitable: bool
    requested_quantity: int
    supported_quantity: int
    limiting_leg: Optional[str]
    limiting_leg_depth: int
    category: EconomicAttributionCategory
    explanation: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "side": self.side,
            "guaranteed_payout_cents": self.guaranteed_payout_cents,
            "acquisition_cost_cents": self.acquisition_cost_cents,
            "gross_edge_cents": self.gross_edge_cents,
            "is_gross_profitable": self.is_gross_profitable,
            "estimated_fees_cents": self.estimated_fees_cents,
            "net_edge_cents": self.net_edge_cents,
            "is_net_profitable": self.is_net_profitable,
            "requested_quantity": self.requested_quantity,
            "supported_quantity": self.supported_quantity,
            "limiting_leg": self.limiting_leg,
            "limiting_leg_depth": self.limiting_leg_depth,
            "category": self.category.value,
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class MicrostructureAttribution:
    """Underlying order-book spreads, depth, and invariant validation."""

    sum_yes_bids_cents: int
    sum_yes_asks_cents: int
    sum_no_bids_cents: int
    sum_no_asks_cents: int
    aggregate_spread_cents: int
    mean_leg_spread_cents: float
    max_leg_spread_cents: int
    min_depth_contracts: int
    max_depth_contracts: int
    complementarity_invariant_holds: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sum_yes_bids_cents": self.sum_yes_bids_cents,
            "sum_yes_asks_cents": self.sum_yes_asks_cents,
            "sum_no_bids_cents": self.sum_no_bids_cents,
            "sum_no_asks_cents": self.sum_no_asks_cents,
            "aggregate_spread_cents": self.aggregate_spread_cents,
            "mean_leg_spread_cents": round(self.mean_leg_spread_cents, 2),
            "max_leg_spread_cents": self.max_leg_spread_cents,
            "min_depth_contracts": self.min_depth_contracts,
            "max_depth_contracts": self.max_depth_contracts,
            "complementarity_invariant_holds": self.complementarity_invariant_holds,
        }


@dataclass(frozen=True)
class SnapshotEconomicAttribution:
    """Comprehensive economic attribution for a reconstructed MECE snapshot."""

    snapshot_id: str
    event_ticker: str
    basket_id: str
    num_legs: int
    market_tickers: Tuple[str, ...]
    outcomes: Tuple[str, ...]
    snapshot_timestamp: Optional[str]
    leg_timestamp_skew_seconds: float
    is_valid_snapshot: bool
    market_state_fingerprint: Optional[str]
    yes_side: BasketSideAttribution
    no_side: BasketSideAttribution
    microstructure: MicrostructureAttribution
    dominant_failure_reason: str
    detailed_economic_diagnostic: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "event_ticker": self.event_ticker,
            "basket_id": self.basket_id,
            "num_legs": self.num_legs,
            "market_tickers": list(self.market_tickers),
            "outcomes": list(self.outcomes),
            "snapshot_timestamp": self.snapshot_timestamp,
            "leg_timestamp_skew_seconds": self.leg_timestamp_skew_seconds,
            "is_valid_snapshot": self.is_valid_snapshot,
            "market_state_fingerprint": self.market_state_fingerprint,
            "yes_side": self.yes_side.to_dict(),
            "no_side": self.no_side.to_dict(),
            "microstructure": self.microstructure.to_dict(),
            "dominant_failure_reason": self.dominant_failure_reason,
            "detailed_economic_diagnostic": self.detailed_economic_diagnostic,
        }


class OpportunityEconomicsAnalyzer:
    """Deterministic opportunity attribution engine for MECE basket candidates."""

    def __init__(self, fee_model_config: Optional[FeeModelConfig] = None) -> None:
        self.fee_config = fee_model_config or FeeModelConfig(
            schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER
        )

    def analyze_snapshot(
        self,
        snapshot: ReconstructedBasketSnapshot,
        requested_quantity: int = 1,
    ) -> SnapshotEconomicAttribution:
        """Perform comprehensive two-sided economic attribution on a reconstructed snapshot."""
        basket = snapshot.basket
        ordered_tickers = basket.ordered_tickers
        num_legs = len(ordered_tickers)

        # 1. Handle Invalid Snapshot upfront (Data quality / skew failure)
        if not snapshot.is_valid:
            dummy_side = BasketSideAttribution(
                side=YES,
                guaranteed_payout_cents=0,
                acquisition_cost_cents=0,
                gross_edge_cents=0,
                is_gross_profitable=False,
                estimated_fees_cents=None,
                net_edge_cents=None,
                is_net_profitable=False,
                requested_quantity=requested_quantity,
                supported_quantity=0,
                limiting_leg=None,
                limiting_leg_depth=0,
                category=EconomicAttributionCategory.DATA_QUALITY_OR_SKEW,
                explanation=snapshot.rejection_reason or snapshot.status,
            )
            dummy_no = BasketSideAttribution(
                side=NO,
                guaranteed_payout_cents=0,
                acquisition_cost_cents=0,
                gross_edge_cents=0,
                is_gross_profitable=False,
                estimated_fees_cents=None,
                net_edge_cents=None,
                is_net_profitable=False,
                requested_quantity=requested_quantity,
                supported_quantity=0,
                limiting_leg=None,
                limiting_leg_depth=0,
                category=EconomicAttributionCategory.DATA_QUALITY_OR_SKEW,
                explanation=snapshot.rejection_reason or snapshot.status,
            )
            dummy_micro = MicrostructureAttribution(
                sum_yes_bids_cents=0,
                sum_yes_asks_cents=0,
                sum_no_bids_cents=0,
                sum_no_asks_cents=0,
                aggregate_spread_cents=0,
                mean_leg_spread_cents=0.0,
                max_leg_spread_cents=0,
                min_depth_contracts=0,
                max_depth_contracts=0,
                complementarity_invariant_holds=True,
            )
            return SnapshotEconomicAttribution(
                snapshot_id=snapshot.snapshot_id,
                event_ticker=basket.event_ticker,
                basket_id=basket.basket_id or f"BASKET-{basket.event_ticker}",
                num_legs=num_legs,
                market_tickers=ordered_tickers,
                outcomes=basket.outcomes,
                snapshot_timestamp=snapshot.max_timestamp.isoformat() if snapshot.max_timestamp else None,
                leg_timestamp_skew_seconds=snapshot.leg_timestamp_skew_seconds,
                is_valid_snapshot=False,
                market_state_fingerprint=snapshot.market_state_fingerprint,
                yes_side=dummy_side,
                no_side=dummy_no,
                microstructure=dummy_micro,
                dominant_failure_reason=snapshot.status,
                detailed_economic_diagnostic=f"Data pipeline rejection: {snapshot.rejection_reason or snapshot.status}",
            )

        # 2. Extract books and build portfolio Event
        event = Event(
            identifier=basket.event_ticker,
            outcomes=basket.outcomes,
            relationship_established=True,
        )
        books_by_outcome: Dict[str, NormalizedOrderBook] = {}
        for t in ordered_tickers:
            out = basket.market_outcome_map[t]
            obs = snapshot.observations.get(t)
            if obs and obs.order_book:
                books_by_outcome[out] = obs.order_book

        # 3. Evaluate Long YES construction
        pricing_yes = evaluate_mece_basket_execution(
            event=event,
            books_by_outcome=books_by_outcome,
            basket_side=YES,
            requested_quantity=requested_quantity,
            fee_model=self.fee_config,
            series_ticker=basket.event_ticker,
        )
        yes_attr = self._classify_side_attribution(
            side=YES,
            pricing=pricing_yes,
            requested_qty=requested_quantity,
            ordered_tickers=ordered_tickers,
            basket=basket,
            books_by_outcome=books_by_outcome,
        )

        # 4. Evaluate Long NO construction
        pricing_no = evaluate_mece_basket_execution(
            event=event,
            books_by_outcome=books_by_outcome,
            basket_side=NO,
            requested_quantity=requested_quantity,
            fee_model=self.fee_config,
            series_ticker=basket.event_ticker,
        )
        no_attr = self._classify_side_attribution(
            side=NO,
            pricing=pricing_no,
            requested_qty=requested_quantity,
            ordered_tickers=ordered_tickers,
            basket=basket,
            books_by_outcome=books_by_outcome,
        )

        # 5. Extract order-book microstructure & verify mathematical invariant
        micro = self._analyze_microstructure(ordered_tickers, snapshot.observations)

        # Invariant check: Gross Edge YES + Gross Edge NO == -Aggregate Spread
        # (For requested quantity = 1)
        expected_combined_edge = -(micro.aggregate_spread_cents * requested_quantity)
        actual_combined_edge = pricing_yes.gross_profit_cents + pricing_no.gross_profit_cents
        invariant_holds = (actual_combined_edge == expected_combined_edge)

        # Update micro with actual checked invariant
        micro = MicrostructureAttribution(
            sum_yes_bids_cents=micro.sum_yes_bids_cents,
            sum_yes_asks_cents=micro.sum_yes_asks_cents,
            sum_no_bids_cents=micro.sum_no_bids_cents,
            sum_no_asks_cents=micro.sum_no_asks_cents,
            aggregate_spread_cents=micro.aggregate_spread_cents,
            mean_leg_spread_cents=micro.mean_leg_spread_cents,
            max_leg_spread_cents=micro.max_leg_spread_cents,
            min_depth_contracts=micro.min_depth_contracts,
            max_depth_contracts=micro.max_depth_contracts,
            complementarity_invariant_holds=invariant_holds,
        )

        # 6. Synthesize dominant diagnostic
        diagnostic, dominant_reason = self._synthesize_diagnostic(yes_attr, no_attr, micro, num_legs)

        return SnapshotEconomicAttribution(
            snapshot_id=snapshot.snapshot_id,
            event_ticker=basket.event_ticker,
            basket_id=basket.basket_id or f"BASKET-{basket.event_ticker}",
            num_legs=num_legs,
            market_tickers=ordered_tickers,
            outcomes=basket.outcomes,
            snapshot_timestamp=snapshot.max_timestamp.isoformat() if snapshot.max_timestamp else None,
            leg_timestamp_skew_seconds=snapshot.leg_timestamp_skew_seconds,
            is_valid_snapshot=True,
            market_state_fingerprint=snapshot.market_state_fingerprint,
            yes_side=yes_attr,
            no_side=no_attr,
            microstructure=micro,
            dominant_failure_reason=dominant_reason,
            detailed_economic_diagnostic=diagnostic,
        )

    def analyze_snapshots(
        self,
        snapshots: Sequence[ReconstructedBasketSnapshot],
        requested_quantity: int = 1,
    ) -> List[SnapshotEconomicAttribution]:
        """Analyze a list of reconstructed snapshots."""
        return [self.analyze_snapshot(s, requested_quantity) for s in snapshots]

    def _classify_side_attribution(
        self,
        side: str,
        pricing: ExecutionPricingResult,
        requested_qty: int,
        ordered_tickers: Sequence[str],
        basket: Any,
        books_by_outcome: Mapping[str, NormalizedOrderBook],
    ) -> BasketSideAttribution:
        """Classify causal rejection category and identify limiting bottleneck for a side."""
        payout = pricing.guaranteed_payout_cents
        cost = pricing.total_cost_cents
        gross_edge = pricing.gross_profit_cents
        net_edge = pricing.net_profit_cents
        fees = pricing.estimated_fees_cents
        supported_qty = pricing.supported_quantity

        # Find limiting leg depth
        min_depth = 999999999
        limiting_leg = None
        for t in ordered_tickers:
            out = basket.market_outcome_map[t]
            b = books_by_outcome.get(out)
            if b:
                asks = derive_ask_levels(b, side)
                total_leg_qty = sum(int(lvl.quantity) for lvl in asks)
            else:
                total_leg_qty = 0
            if total_leg_qty < min_depth:
                min_depth = total_leg_qty
                limiting_leg = t

        if min_depth == 999999999:
            min_depth = 0

        # Causal classification logic
        if supported_qty == 0:
            category = EconomicAttributionCategory.INSUFFICIENT_DEPTH
            explanation = f"Zero depth available on limiting leg {limiting_leg}"
        elif gross_edge <= 0:
            category = EconomicAttributionCategory.NEGATIVE_GROSS_EDGE
            explanation = (
                f"Negative gross edge of {gross_edge}c: cost ({cost}c) >= guaranteed payout ({payout}c)"
            )
        elif net_edge is not None and net_edge <= 0:
            category = EconomicAttributionCategory.FEE_EROSION
            explanation = (
                f"Gross edge (+{gross_edge}c) eroded to {net_edge}c by exchange fees of {fees}c"
            )
        elif supported_qty < requested_qty:
            category = EconomicAttributionCategory.INSUFFICIENT_DEPTH
            explanation = (
                f"Positive net edge (+{net_edge}c), but depth supports only {supported_qty}/{requested_qty} contracts "
                f"(bottleneck leg: {limiting_leg})"
            )
        else:
            category = EconomicAttributionCategory.QUALIFIED_ARBITRAGE
            explanation = (
                f"Fully qualified candidate: net edge +{net_edge}c for {supported_qty} contracts"
            )

        return BasketSideAttribution(
            side=side,
            guaranteed_payout_cents=payout,
            acquisition_cost_cents=cost,
            gross_edge_cents=gross_edge,
            is_gross_profitable=pricing.is_gross_profitable,
            estimated_fees_cents=fees,
            net_edge_cents=net_edge,
            is_net_profitable=bool(pricing.is_net_profitable),
            requested_quantity=requested_qty,
            supported_quantity=supported_qty,
            limiting_leg=limiting_leg,
            limiting_leg_depth=min_depth,
            category=category,
            explanation=explanation,
        )

    def _analyze_microstructure(
        self,
        ordered_tickers: Sequence[str],
        observations: Mapping[str, Any],
    ) -> MicrostructureAttribution:
        """Extract top-of-book bid, ask, spread, and depth metrics across constituent legs."""
        yes_bids = []
        yes_asks = []
        spreads = []
        depths = []

        for t in ordered_tickers:
            obs = observations.get(t)
            m = obs.market if obs else None
            b = obs.order_book if obs else None

            # YES bid/ask in cents
            y_bid = 0
            y_ask = 100
            if m:
                if m.yes_bid_dollars is not None:
                    y_bid = dollars_to_cents(m.yes_bid_dollars)
                if m.yes_ask_dollars is not None:
                    y_ask = dollars_to_cents(m.yes_ask_dollars)

            spread = max(0, y_ask - y_bid)
            spreads.append(spread)
            yes_bids.append(y_bid)
            yes_asks.append(y_ask)

            if b:
                y_depth = sum(int(lvl.quantity) for lvl in derive_ask_levels(b, YES))
                depths.append(y_depth)
            else:
                depths.append(0)

        sum_y_bids = sum(yes_bids)
        sum_y_asks = sum(yes_asks)
        # In binary markets, NO ask = 100 - YES bid, NO bid = 100 - YES ask
        sum_n_asks = len(ordered_tickers) * 100 - sum_y_bids
        sum_n_bids = len(ordered_tickers) * 100 - sum_y_asks
        agg_spread = sum(spreads)
        mean_spread = (agg_spread / len(spreads)) if spreads else 0.0
        max_spread = max(spreads) if spreads else 0
        min_depth = min(depths) if depths else 0
        max_depth = max(depths) if depths else 0

        return MicrostructureAttribution(
            sum_yes_bids_cents=sum_y_bids,
            sum_yes_asks_cents=sum_y_asks,
            sum_no_bids_cents=sum_n_bids,
            sum_no_asks_cents=sum_n_asks,
            aggregate_spread_cents=agg_spread,
            mean_leg_spread_cents=mean_spread,
            max_leg_spread_cents=max_spread,
            min_depth_contracts=min_depth,
            max_depth_contracts=max_depth,
            complementarity_invariant_holds=True,  # Will be verified against pricing results
        )

    def _synthesize_diagnostic(
        self,
        yes_attr: BasketSideAttribution,
        no_attr: BasketSideAttribution,
        micro: MicrostructureAttribution,
        num_legs: int,
    ) -> Tuple[str, str]:
        """Synthesize a human-readable economic diagnostic and dominant failure reason."""
        if yes_attr.category == EconomicAttributionCategory.QUALIFIED_ARBITRAGE:
            return "Qualified Long YES arbitrage opportunity", "QUALIFIED_YES"
        if no_attr.category == EconomicAttributionCategory.QUALIFIED_ARBITRAGE:
            return "Qualified Long NO arbitrage opportunity", "QUALIFIED_NO"

        # Check for market over-round / under-round regime
        # Normal Over-Round: Sum(YES_Ask) > 100 and Sum(YES_Bid) < 100
        if micro.sum_yes_asks_cents > 100 and micro.sum_yes_bids_cents < 100:
            dominant = "NORMAL_OVER_ROUND_MARKET_MAKING"
            diag = (
                f"Market is in normal over-round quoting: Sum(YES_Ask) = {micro.sum_yes_asks_cents}c > 100c "
                f"(Long YES edge: {yes_attr.gross_edge_cents}c), while Sum(YES_Bid) = {micro.sum_yes_bids_cents}c < 100c "
                f"(Long NO edge: {no_attr.gross_edge_cents}c). Aggregate spread penalty is {micro.aggregate_spread_cents}c "
                f"across {num_legs} legs (mean {micro.mean_leg_spread_cents:.1f}c/leg). Fair probability sits inside the spread."
            )
            return diag, dominant

        # Candidate had positive gross edge on one side, but failed on fees or depth
        if yes_attr.is_gross_profitable:
            dominant = f"YES_{yes_attr.category.value}"
            diag = f"Long YES had positive gross edge ({yes_attr.gross_edge_cents}c) but failed: {yes_attr.explanation}"
            return diag, dominant

        if no_attr.is_gross_profitable:
            dominant = f"NO_{no_attr.category.value}"
            diag = f"Long NO had positive gross edge ({no_attr.gross_edge_cents}c) but failed: {no_attr.explanation}"
            return diag, dominant

        dominant = "NEGATIVE_GROSS_EDGE_BOTH_SIDES"
        diag = (
            f"Both sides show negative gross edge (YES: {yes_attr.gross_edge_cents}c, NO: {no_attr.gross_edge_cents}c). "
            f"Aggregate book spread is {micro.aggregate_spread_cents}c."
        )
        return diag, dominant

    def summarize_attributions(
        self,
        attributions: Sequence[SnapshotEconomicAttribution],
    ) -> Dict[str, Any]:
        """Compute aggregate attribution statistics across evaluated snapshots."""
        total = len(attributions)
        if total == 0:
            return {"total_snapshots": 0}

        yes_categories: Dict[str, int] = {}
        no_categories: Dict[str, int] = {}
        dominant_reasons: Dict[str, int] = {}
        yes_gross_edges: List[int] = []
        no_gross_edges: List[int] = []
        yes_net_edges: List[int] = []
        no_net_edges: List[int] = []
        spreads: List[int] = []
        invariant_pass = 0

        for a in attributions:
            y_cat = a.yes_side.category.value
            n_cat = a.no_side.category.value
            yes_categories[y_cat] = yes_categories.get(y_cat, 0) + 1
            no_categories[n_cat] = no_categories.get(n_cat, 0) + 1
            dominant_reasons[a.dominant_failure_reason] = dominant_reasons.get(a.dominant_failure_reason, 0) + 1

            if a.is_valid_snapshot:
                yes_gross_edges.append(a.yes_side.gross_edge_cents)
                no_gross_edges.append(a.no_side.gross_edge_cents)
                if a.yes_side.net_edge_cents is not None:
                    yes_net_edges.append(a.yes_side.net_edge_cents)
                if a.no_side.net_edge_cents is not None:
                    no_net_edges.append(a.no_side.net_edge_cents)
                spreads.append(a.microstructure.aggregate_spread_cents)
                if a.microstructure.complementarity_invariant_holds:
                    invariant_pass += 1

        return {
            "total_snapshots_analyzed": total,
            "valid_snapshots": len(yes_gross_edges),
            "complementarity_invariant_pass_rate": (invariant_pass / len(yes_gross_edges)) if yes_gross_edges else 0.0,
            "dominant_reasons_breakdown": dominant_reasons,
            "yes_side_categories": yes_categories,
            "no_side_categories": no_categories,
            "yes_gross_edge_range_cents": [min(yes_gross_edges), max(yes_gross_edges)] if yes_gross_edges else None,
            "no_gross_edge_range_cents": [min(no_gross_edges), max(no_gross_edges)] if no_gross_edges else None,
            "yes_net_edge_range_cents": [min(yes_net_edges), max(yes_net_edges)] if yes_net_edges else None,
            "no_net_edge_range_cents": [min(no_net_edges), max(no_net_edges)] if no_net_edges else None,
            "aggregate_spread_range_cents": [min(spreads), max(spreads)] if spreads else None,
            "mean_aggregate_spread_cents": round(sum(spreads) / len(spreads), 2) if spreads else 0.0,
        }
