"""Autonomous paper execution manager using V8 realistic multi-leg simulation.

V9 Execution Manager:
- Bridges risk-approved candidates to V8 RealisticExecutionSimulator.
- Accurately maps order-book bids and derived asks into multi-leg depth tiers.
- Enforces V8 microstructure latency, quote-decay, and unwind recovery.
- Reconciles fills, fees, and liquidations directly into PaperPortfolio.
- Enforces post-execution reconciliation check after every trade.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
import uuid

from .arbitrage import (
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
    dollars_to_cents,
)
from .async_execution import (
    AsyncExecutionError,
    AsyncMultiLegTrade,
    LegSequencingPolicy,
    MicrostructureConfig,
    RealisticExecutionSimulator,
    UnwindPolicy,
    apply_async_trade_to_portfolio,
)
from .contract import NO, YES
from .execution import derive_ask_levels
from .fee_model import FeeModelConfig
from .ledger import LedgerError, PaperPortfolio, ReconciliationResult
from .market_data import NormalizedOrderBook
from .opportunity_evaluator import EvaluatedCandidate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaperExecutionConfig:
    """Configuration for simulated execution environment."""

    microstructure_config: MicrostructureConfig = field(default_factory=MicrostructureConfig)
    unwind_policy: UnwindPolicy = UnwindPolicy.AGGRESSIVE_CROSS
    sequencing_policy: LegSequencingPolicy = LegSequencingPolicy.ILLIQUID_FIRST


class PaperExecutionManager:
    """Manages the full simulated electronic execution lifecycle for approved trades."""

    def __init__(self, config: Optional[PaperExecutionConfig] = None) -> None:
        self.config = config or PaperExecutionConfig()
        self.simulator = RealisticExecutionSimulator(
            config=self.config.microstructure_config
        )

    def execute_candidate(
        self,
        candidate: EvaluatedCandidate,
        order_book: NormalizedOrderBook,
        portfolio: PaperPortfolio,
        simulated_time: Optional[datetime] = None,
    ) -> AsyncMultiLegTrade:
        """Executes a risk-approved arbitrage candidate against observed order-book depth."""
        now = simulated_time or datetime.now(timezone.utc)
        ticker = candidate.market_ticker

        # 1. Prepare depth and counter-party bids
        strat = candidate.strategy_type.lower()
        if strat in (OPPORTUNITY_BINARY_PARITY, "binary_parity"):
            legs_specs = [
                (ticker, ticker, YES),
                (ticker, ticker, NO),
            ]
            # Derived asks:
            # YES ask from NO bids: ask_p = 100 - no_bid
            yes_asks = [(100 - dollars_to_cents(lvl.price_dollars), lvl.quantity) for lvl in order_book.no_bids]
            # NO ask from YES bids: ask_p = 100 - yes_bid
            no_asks = [(100 - dollars_to_cents(lvl.price_dollars), lvl.quantity) for lvl in order_book.yes_bids]

            yes_bids = [(dollars_to_cents(lvl.price_dollars), lvl.quantity) for lvl in order_book.yes_bids]
            no_bids = [(dollars_to_cents(lvl.price_dollars), lvl.quantity) for lvl in order_book.no_bids]

            available_depth = {
                ticker: yes_asks,  # Leg YES asks
            }
            # For leg NO, we need separate key if contract_id differs, but here both are on ticker.
            # To distinguish leg depths in simulator, use side-augmented keys or simulator leg matching
            # RealisticExecutionSimulator matches available_depth_by_leg by contract_id.
            # In single-market binary parity, contract_id is ticker. Let's use (ticker, side) as contract_id!
            legs_specs = [
                (f"{ticker}:YES", ticker, YES),
                (f"{ticker}:NO", ticker, NO),
            ]
            available_depth = {
                f"{ticker}:YES": yes_asks,
                f"{ticker}:NO": no_asks,
            }
            counterparty_bids = {
                f"{ticker}:YES": yes_bids,
                f"{ticker}:NO": no_bids,
            }

        else:
            raise AsyncExecutionError(f"Unsupported strategy type {candidate.strategy_type}")

        trade_id = f"trd-{uuid.uuid4().hex[:12]}"
        target_qty = candidate.supported_quantity

        # 2. Simulate multi-leg execution via V8 simulator
        trade = self.simulator.simulate_execution(
            trade_id=trade_id,
            strategy_type=candidate.strategy_type,
            target_quantity=target_qty,
            legs_specs=legs_specs,
            available_depth_by_leg=available_depth,
            counterparty_bids_by_leg=counterparty_bids,
            start_time=now,
        )

        # 3. Apply execution results to PaperPortfolio
        apply_async_trade_to_portfolio(portfolio, trade, timestamp=now.isoformat())

        # 4. Immediate post-trade reconciliation audit
        rec = portfolio.reconcile()
        if not rec.is_reconciled:
            raise LedgerError(
                f"Ledger reconciliation failed immediately after executing trade {trade_id}: "
                f"{rec.discrepancies}"
            )

        logger.info(
            f"Executed simulated paper trade {trade_id} ({trade.state.value}): "
            f"target={target_qty}, cost={trade.total_cost_cents}¢, fees={trade.total_fees_cents}¢, "
            f"arbitrage_complete={trade.is_arbitrage_complete}"
        )
        return trade
