"""Deterministic opportunity detection, depth-aware evaluation, and candidate ranking.

V9 Opportunity Evaluator:
- Combines V2 arbitrage detection, V3 depth traversal, and V8 CFTC quadratic fee modeling.
- Deterministic candidate generation and qualification.
- Strict distinction between theoretical edge, execution feasibility, and net profitability.
- Deterministic candidate ranking and duplicate suppression / cooldown.
- Strictly point-in-time; no future quotes or look-ahead.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
import uuid

from .arbitrage import (
    ArbitrageOpportunity,
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
    evaluate_market_parity,
    evaluate_mece_markets,
)
from .contract import NO, YES
from .execution import (
    ExecutionPricingResult,
    STATUS_DEPTH_SUPPORTED_NET_PROFITABLE,
    STATUS_INSUFFICIENT_DEPTH,
    STATUS_NO_GROSS_EDGE,
    evaluate_binary_parity_execution,
    evaluate_mece_basket_execution,
)
from .fee_model import FeeModelConfig, FeeScheduleType
from .market_data import NormalizedMarket, NormalizedOrderBook
from .portfolio import Event
from .universe import MarketLiquidityProfile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvaluatedCandidate:
    """An arbitrage opportunity evaluated against live depth and official fees."""

    candidate_id: str
    strategy_type: str
    market_ticker: str
    evaluated_at: datetime
    execution_result: ExecutionPricingResult
    opportunity: Optional[ArbitrageOpportunity] = None
    liquidity_profile: Optional[MarketLiquidityProfile] = None

    @property
    def is_execution_feasible(self) -> bool:
        """True if the order book supports at least 1 contract unit."""
        return self.execution_result.supported_quantity > 0

    @property
    def is_net_profitable(self) -> bool:
        """True if net profit after fees and slippage is strictly positive."""
        return (
            self.execution_result.is_net_profitable is True
            and self.execution_result.net_profit_cents > 0
        )

    @property
    def net_profit_cents(self) -> int:
        return self.execution_result.net_profit_cents

    @property
    def gross_profit_cents(self) -> int:
        return self.execution_result.gross_profit_cents

    @property
    def total_cost_cents(self) -> int:
        return self.execution_result.total_cost_cents

    @property
    def total_fees_cents(self) -> int:
        return self.execution_result.estimated_fees_cents or 0

    @property
    def supported_quantity(self) -> int:
        return self.execution_result.supported_quantity

    @property
    def rejection_reason(self) -> Optional[str]:
        return self.execution_result.rejection_reason


@dataclass(frozen=True)
class OpportunityEvaluatorConfig:
    """Configuration for opportunity evaluation and ranking."""

    target_quantity: int = 1
    require_net_profitable: bool = True
    min_net_profit_cents: int = 1
    cooldown_cycles: int = 3
    fee_model_config: FeeModelConfig = field(
        default_factory=lambda: FeeModelConfig(schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER)
    )
    max_candidates_per_cycle: int = 5


class AutonomousOpportunityEvaluator:
    """Evaluates, ranks, and deduplicates market arbitrage opportunities."""

    def __init__(self, config: Optional[OpportunityEvaluatorConfig] = None) -> None:
        self.config = config or OpportunityEvaluatorConfig()
        self._last_evaluated_cycle: Dict[str, int] = {}
        self._last_traded_cycle: Dict[str, int] = {}

    def is_in_cooldown(self, ticker: str, current_cycle: int) -> bool:
        """Check if market is under cooldown from previous cycle evaluation."""
        last_traded = self._last_traded_cycle.get(ticker)
        if last_traded is not None and (current_cycle - last_traded) < self.config.cooldown_cycles:
            return True
        return False

    def mark_traded(self, ticker: str, current_cycle: int) -> None:
        """Record that a trade was initiated on this market."""
        self._last_traded_cycle[ticker] = current_cycle

    def evaluate_market_binary_parity(
        self,
        market: NormalizedMarket,
        order_book: NormalizedOrderBook,
        liquidity_profile: Optional[MarketLiquidityProfile] = None,
        current_cycle: int = 0,
        reference_time: Optional[datetime] = None,
    ) -> Optional[EvaluatedCandidate]:
        """Evaluate single-market binary parity for a market and its order book."""
        eval_time = reference_time or datetime.now(timezone.utc)

        # Check cooldown
        if self.is_in_cooldown(market.ticker, current_cycle):
            return None

        # 1. Theoretical opportunity detection (V2)
        opp = evaluate_market_parity(market)

        # 2. Depth and fee evaluation (V3 + V8)
        series = market.series_ticker if hasattr(market, "series_ticker") else None
        exec_res = evaluate_binary_parity_execution(
            order_book,
            requested_quantity=self.config.target_quantity,
            fee_model=self.config.fee_model_config,
            series_ticker=series,
        )

        candidate = EvaluatedCandidate(
            candidate_id=f"cand-{uuid.uuid4().hex[:12]}",
            strategy_type=OPPORTUNITY_BINARY_PARITY,
            market_ticker=market.ticker,
            evaluated_at=eval_time,
            execution_result=exec_res,
            opportunity=opp,
            liquidity_profile=liquidity_profile,
        )

        self._last_evaluated_cycle[market.ticker] = current_cycle
        return candidate

    def rank_candidates(
        self,
        candidates: Sequence[EvaluatedCandidate],
    ) -> Tuple[EvaluatedCandidate, ...]:
        """Deterministically ranks candidates using multi-factor quality criteria.

        Sorting criteria:
        1. Net profit in cents (descending)
        2. Supported executable quantity (descending)
        3. Liquidity score if available (descending)
        4. Acquisition cost in cents (ascending)
        5. Market ticker (lexicographical tie-breaker)
        """
        def _sort_key(c: EvaluatedCandidate):
            score = (
                float(c.liquidity_profile.liquidity_score)
                if (c.liquidity_profile and c.liquidity_profile.liquidity_score is not None)
                else 0.0
            )
            return (
                c.net_profit_cents,
                c.supported_quantity,
                score,
                -c.total_cost_cents,
                c.market_ticker,
            )

        sorted_cands = sorted(candidates, key=_sort_key, reverse=True)
        return tuple(sorted_cands[: self.config.max_candidates_per_cycle])
