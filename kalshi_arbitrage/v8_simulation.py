"""Kalshi Arbitrage Engine — V8 Realistic Execution & Risk Simulation.

Implements the end-to-end V8 simulation and research workflow:
1. Liquid universe selection filtering (V8.5).
2. Arbitrage opportunity detection with CFTC quadratic fee modeling (V8.1) vs flat fee comparison.
3. Realistic asynchronous multi-leg execution lifecycle simulation (V8.2).
4. Microstructure latency modeling and quote decay (V8.3).
5. Legged-out position unwind and scratch recovery accounting (V8.4).
6. Exact ledger reconciliation with PaperPortfolio.
7. Parameter latency sensitivity sweeps.
8. Comprehensive auditable reporting and JSON export.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from .arbitrage import (
    ArbitrageOpportunity,
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
    cents_to_dollars,
    dollars_to_cents,
    evaluate_market_parity,
    evaluate_mece_markets,
)
from .async_execution import (
    AsyncExecutionError,
    AsyncMultiLegTrade,
    AsyncTradeLeg,
    LegExecutionStatus,
    LegSequencingPolicy,
    MicrostructureConfig,
    MultiLegLifecycleState,
    RealisticExecutionSimulator,
    UnwindPolicy,
    apply_async_trade_to_portfolio,
)
from .contract import NO, YES
from .evaluation import HistoricalDataset, _normalize_timestamp
from .execution import (
    ExecutionPricingResult,
    STATUS_DEPTH_SUPPORTED_NET_PROFITABLE,
    STATUS_INSUFFICIENT_DEPTH,
    STATUS_NO_GROSS_EDGE,
    derive_ask_levels,
    evaluate_binary_parity_execution,
    evaluate_mece_basket_execution,
)
from .fee_model import (
    FeeModelConfig,
    FeeScheduleType,
    FillTier,
    calculate_multi_level_fee,
    calculate_single_level_fee,
)
from .ledger import PaperPortfolio, ReconciliationResult
from .market_data import NormalizedMarket, NormalizedOrderBook, OrderBookLevel
from .observer import DeclaredMeceBasket, MarketObservation
from .universe import (
    LiquidUniverseSelector,
    MarketLiquidityProfile,
    UniverseFilterConfig,
    UniverseSelectionResult,
)


def _pct(num: int, denom: int) -> Decimal:
    """Safe percentage calculation formatted to 2 decimal places."""
    if denom <= 0:
        return Decimal("0.00")
    val = (Decimal(num) / Decimal(denom)) * Decimal("100.0")
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# V8 Simulation Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V8SimulationConfig:
    """Configuration for V8 realistic execution and risk simulation."""

    universe_config: UniverseFilterConfig = field(default_factory=UniverseFilterConfig)
    microstructure_config: MicrostructureConfig = field(default_factory=MicrostructureConfig)
    fee_config: FeeModelConfig = field(default_factory=FeeModelConfig)
    flat_fee_per_contract_cents: int = 1
    unwind_policy: UnwindPolicy = UnwindPolicy.AGGRESSIVE_CROSS
    leg_sequencing: LegSequencingPolicy = LegSequencingPolicy.ILLIQUID_FIRST
    target_quantity: int = 1
    initial_cash_cents: int = 1_000_000  # $10,000.00
    enforce_universe: bool = True
    max_stale_seconds: float = 60.0
    sensitivity_latencies_ms: Tuple[float, ...] = (0.0, 50.0, 100.0, 250.0, 500.0, 1000.0)


# ---------------------------------------------------------------------------
# Individual Simulated Record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V8OpportunityRecord:
    """Audit record for an evaluated opportunity under V8 execution simulation."""

    opportunity_id: str
    observed_at: datetime
    strategy_type: str
    market_tickers: Tuple[str, ...]
    in_universe: bool
    universe_rejection_reasons: Tuple[str, ...]
    gross_edge_cents: int
    flat_net_edge_cents: int
    cftc_net_edge_cents: int
    is_fee_reversal: bool
    is_cftc_advantage: bool
    executed: bool
    execution_state: Optional[str]
    is_arbitrage_complete: bool
    target_quantity: int
    filled_quantity: int
    gross_pnl_cents: int
    fees_paid_cents: int
    scratch_loss_cents: int
    net_realized_pnl_cents: int
    residual_exposure: Mapping[str, int]
    async_trade: Optional[AsyncMultiLegTrade] = None


# ---------------------------------------------------------------------------
# Summary Metrics & Sensitivity Points
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V8FeeDiscrepancyMetrics:
    """Quantitative comparison between flat fee assumptions and CFTC quadratic fees."""

    total_detected_opportunities: int
    flat_fee_profitable_count: int
    cftc_fee_profitable_count: int
    fee_reversal_count: int
    fee_reversal_pct: Decimal
    cftc_advantage_count: int
    cftc_advantage_pct: Decimal
    total_theoretical_flat_fees_cents: int
    total_theoretical_cftc_fees_cents: int


@dataclass(frozen=True)
class V8LatencySensitivityPoint:
    """Performance point for a specific network/execution latency parameter."""

    latency_ms: float
    trades_attempted: int
    trades_fully_filled: int
    trades_legged_out: int
    trades_failed: int
    completion_rate_pct: Decimal
    legged_out_rate_pct: Decimal
    net_realized_pnl_cents: int
    total_scratch_loss_cents: int


# ---------------------------------------------------------------------------
# V8 Simulation Report
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V8SimulationReport:
    """Comprehensive auditable report for V8 realistic execution and risk simulation."""

    config: V8SimulationConfig
    dataset_summary: Dict[str, Any]
    universe_result: UniverseSelectionResult
    fee_metrics: V8FeeDiscrepancyMetrics
    records: Tuple[V8OpportunityRecord, ...]
    trades_attempted: int
    trades_fully_filled: int
    trades_legged_out: int
    trades_failed: int
    completion_rate_pct: Decimal
    legged_out_rate_pct: Decimal
    target_contracts_total: int
    filled_contracts_total: int
    initial_cash_cents: int
    final_cash_cents: int
    final_equity_cents: int
    realized_gross_edge_cents: int
    total_exchange_fees_cents: int
    total_scratch_loss_cents: int
    net_realized_pnl_cents: int
    max_drawdown_cents: int
    max_drawdown_pct: Decimal
    ledger_is_reconciled: bool
    reconciliation_discrepancies: Tuple[str, ...]
    latency_sensitivity: Tuple[V8LatencySensitivityPoint, ...]
    async_trades: Tuple[AsyncMultiLegTrade, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        """Render a publication-quality ASCII report."""
        ds = self.dataset_summary
        fm = self.fee_metrics
        cfg = self.config

        lines = [
            "=" * 78,
            "KALSHI ARBITRAGE ENGINE — V8 REALISTIC EXECUTION & RISK SIMULATION",
            "=" * 78,
            "1. CONFIGURATION & MICROSTRUCTURE ASSUMPTIONS:",
            f"  Fee Schedule Model:                {cfg.fee_config.schedule_type.value.upper()}",
            f"  Flat Baseline Fee:                 {cfg.flat_fee_per_contract_cents}¢ / contract",
            f"  Base Network Latency:              {cfg.microstructure_config.network_latency_ms:.1f}ms",
            f"  Sequential Leg Delay:              {cfg.microstructure_config.sequential_leg_delay_ms:.1f}ms",
            f"  Leg Sequencing Policy:             {cfg.leg_sequencing.value}",
            f"  Legged-Out Unwind Policy:          {cfg.unwind_policy.value}",
            f"  Liquid Universe Filter:            {'ENFORCED' if cfg.enforce_universe else 'PERMISSIVE'}",
            "",
            "2. DATASET PROVENANCE:",
            f"  Source:                            {ds.get('source_description', 'N/A')}",
            f"  Total Raw Records:                 {ds.get('total_raw_records', 0)}",
            f"  Valid Observations:                {ds.get('valid_observations_count', 0)}",
            f"  Span:                              {ds.get('earliest_timestamp', 'None')} -> {ds.get('latest_timestamp', 'None')}",
            f"  Unique Tickers Observed:           {ds.get('unique_tickers_count', 0)}",
            "",
            "3. V8.5 LIQUID UNIVERSE FILTERING:",
            f"  Markets Evaluated:                 {self.universe_result.total_markets_evaluated}",
            f"  Qualified Liquid Markets:          {self.universe_result.qualified_markets_count}",
            f"  Rejected Illiquid Markets:         {self.universe_result.rejected_markets_count}",
        ]

        if self.universe_result.rejections_by_reason:
            lines.append("  Rejection Breakdown:")
            for reason, count in sorted(self.universe_result.rejections_by_reason.items()):
                lines.append(f"    - {reason}: {count}")

        lines.extend([
            "",
            "4. V8.1 FEE DISCREPANCY & REVERSAL ANALYSIS:",
            f"  Detected Opportunities:            {fm.total_detected_opportunities}",
            f"  Profitable Under Flat 1¢ Fee:      {fm.flat_fee_profitable_count}",
            f"  Profitable Under CFTC Quadratic:   {fm.cftc_fee_profitable_count}",
            f"  Fee Reversals (Flat Win -> CFTC Loss): {fm.fee_reversal_count} ({fm.fee_reversal_pct}%)",
            f"  CFTC Fee Advantages:               {fm.cftc_advantage_count} ({fm.cftc_advantage_pct}%)",
            f"  Theoretical Total Flat Fees:       ${fm.total_theoretical_flat_fees_cents / 100:.2f}",
            f"  Theoretical Total CFTC Fees:       ${fm.total_theoretical_cftc_fees_cents / 100:.2f}",
            "",
            "5. V8.2–V8.4 MULTI-LEG ASYNCHRONOUS EXECUTION & UNWIND LIFECYCLE:",
            f"  Trades Attempted:                  {self.trades_attempted}",
            f"  Fully Filled Arbitrages:           {self.trades_fully_filled} ({self.completion_rate_pct}%)",
            f"  Legged-Out (Unwound / Scratched):  {self.trades_legged_out} ({self.legged_out_rate_pct}%)",
            f"  Execution Failed (Zero Fill):      {self.trades_failed}",
            f"  Target vs Filled Contracts:        {self.filled_contracts_total} / {self.target_contracts_total}",
            "",
            "6. FINANCIAL ACCOUNTING & LEDGER RECONCILIATION:",
            f"  Initial Capital:                   ${self.initial_cash_cents / 100:.2f}",
            f"  Final Available Cash:              ${self.final_cash_cents / 100:.2f}",
            f"  Final Portfolio Equity:            ${self.final_equity_cents / 100:.2f}",
            f"  Realized Gross Edge:               {self.realized_gross_edge_cents:+d}¢ (${self.realized_gross_edge_cents / 100:+.2f})",
            f"  Exchange Fees Paid:                {self.total_exchange_fees_cents}¢ (${self.total_exchange_fees_cents / 100:.2f})",
            f"  Scratch / Unwind Drag:             {self.total_scratch_loss_cents:+d}¢ (${self.total_scratch_loss_cents / 100:+.2f})",
            f"  Net Realized P&L:                  {self.net_realized_pnl_cents:+d}¢ (${self.net_realized_pnl_cents / 100:+.2f})",
            f"  Max Drawdown:                      {self.max_drawdown_cents}¢ ({self.max_drawdown_pct}%)",
            f"  Paper Ledger Reconciled:           {'PASS (0 discrepancies)' if self.ledger_is_reconciled else 'FAIL'}",
        ])

        if self.reconciliation_discrepancies:
            for disc in self.reconciliation_discrepancies:
                lines.append(f"    ! {disc}")

        lines.extend([
            "",
            "7. LATENCY SENSITIVITY SWEEP:",
            "  Latency   Attempted  Completed  Legged-Out  Comp %   Leg-Out %   Net P&L    Scratch Drag",
            "  -------   ---------  ---------  ----------  ------   ---------   -------    ------------",
        ])

        for pt in self.latency_sensitivity:
            lines.append(
                f"  {pt.latency_ms:>5.0f}ms   "
                f"{pt.trades_attempted:>9d}  "
                f"{pt.trades_fully_filled:>9d}  "
                f"{pt.trades_legged_out:>10d}  "
                f"{pt.completion_rate_pct:>5.1f}%  "
                f"{pt.legged_out_rate_pct:>7.1f}%  "
                f"{pt.net_realized_pnl_cents:>+6d}¢   "
                f"{pt.total_scratch_loss_cents:>+8d}¢"
            )

        lines.append("=" * 78)
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Convert entire report to a JSON-serializable dictionary."""
        return {
            "config": {
                "fee_schedule": self.config.fee_config.schedule_type.value,
                "flat_fee_per_contract_cents": self.config.flat_fee_per_contract_cents,
                "network_latency_ms": self.config.microstructure_config.network_latency_ms,
                "sequential_leg_delay_ms": self.config.microstructure_config.sequential_leg_delay_ms,
                "sequencing_policy": self.config.leg_sequencing.value,
                "unwind_policy": self.config.unwind_policy.value,
                "enforce_universe": self.config.enforce_universe,
                "target_quantity": self.config.target_quantity,
                "initial_cash_cents": self.config.initial_cash_cents,
            },
            "dataset_summary": self.dataset_summary,
            "universe_selection": {
                "total_evaluated": self.universe_result.total_markets_evaluated,
                "qualified_count": self.universe_result.qualified_markets_count,
                "rejected_count": self.universe_result.rejected_markets_count,
                "rejections_by_reason": dict(self.universe_result.rejections_by_reason),
            },
            "fee_comparison": {
                "total_detected_opportunities": self.fee_metrics.total_detected_opportunities,
                "flat_fee_profitable_count": self.fee_metrics.flat_fee_profitable_count,
                "cftc_fee_profitable_count": self.fee_metrics.cftc_fee_profitable_count,
                "fee_reversal_count": self.fee_metrics.fee_reversal_count,
                "fee_reversal_pct": str(self.fee_metrics.fee_reversal_pct),
                "cftc_advantage_count": self.fee_metrics.cftc_advantage_count,
                "cftc_advantage_pct": str(self.fee_metrics.cftc_advantage_pct),
                "total_flat_fees_cents": self.fee_metrics.total_theoretical_flat_fees_cents,
                "total_cftc_fees_cents": self.fee_metrics.total_theoretical_cftc_fees_cents,
            },
            "execution_lifecycle": {
                "trades_attempted": self.trades_attempted,
                "trades_fully_filled": self.trades_fully_filled,
                "trades_legged_out": self.trades_legged_out,
                "trades_failed": self.trades_failed,
                "completion_rate_pct": str(self.completion_rate_pct),
                "legged_out_rate_pct": str(self.legged_out_rate_pct),
                "target_contracts_total": self.target_contracts_total,
                "filled_contracts_total": self.filled_contracts_total,
            },
            "financial_accounting": {
                "initial_cash_cents": self.initial_cash_cents,
                "final_cash_cents": self.final_cash_cents,
                "final_equity_cents": self.final_equity_cents,
                "realized_gross_edge_cents": self.realized_gross_edge_cents,
                "total_exchange_fees_cents": self.total_exchange_fees_cents,
                "total_scratch_loss_cents": self.total_scratch_loss_cents,
                "net_realized_pnl_cents": self.net_realized_pnl_cents,
                "max_drawdown_cents": self.max_drawdown_cents,
                "max_drawdown_pct": str(self.max_drawdown_pct),
                "ledger_is_reconciled": self.ledger_is_reconciled,
                "reconciliation_discrepancies": list(self.reconciliation_discrepancies),
            },
            "latency_sensitivity": [
                {
                    "latency_ms": pt.latency_ms,
                    "trades_attempted": pt.trades_attempted,
                    "trades_fully_filled": pt.trades_fully_filled,
                    "trades_legged_out": pt.trades_legged_out,
                    "trades_failed": pt.trades_failed,
                    "completion_rate_pct": str(pt.completion_rate_pct),
                    "legged_out_rate_pct": str(pt.legged_out_rate_pct),
                    "net_realized_pnl_cents": pt.net_realized_pnl_cents,
                    "total_scratch_loss_cents": pt.total_scratch_loss_cents,
                }
                for pt in self.latency_sensitivity
            ],
            "records_count": len(self.records),
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize report to indented JSON."""
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# V8 Simulation Engine
# ---------------------------------------------------------------------------

class V8SimulationEngine:
    """Historical replay and realistic execution simulation engine."""

    def __init__(self, config: Optional[V8SimulationConfig] = None) -> None:
        self.config = config or V8SimulationConfig()
        self.universe_selector = LiquidUniverseSelector(config=self.config.universe_config)

    def run(
        self,
        dataset: HistoricalDataset,
        declared_baskets: Sequence[DeclaredMeceBasket] = (),
    ) -> V8SimulationReport:
        """Execute chronological historical replay with V8 realistic execution and risk simulation."""
        # Tracking collections
        records: List[V8OpportunityRecord] = []
        async_trades: List[AsyncMultiLegTrade] = []
        latest_profiles: Dict[str, MarketLiquidityProfile] = {}
        latest_markets: Dict[str, NormalizedMarket] = {}
        latest_books: Dict[str, NormalizedOrderBook] = {}

        portfolio = PaperPortfolio(initial_cash_cents=self.config.initial_cash_cents)

        total_detected = 0
        flat_profitable_count = 0
        cftc_profitable_count = 0
        fee_reversal_count = 0
        cftc_advantage_count = 0
        total_theo_flat_fees = 0
        total_theo_cftc_fees = 0

        trades_attempted = 0
        trades_fully_filled = 0
        trades_legged_out = 0
        trades_failed = 0
        target_contracts_total = 0
        filled_contracts_total = 0
        realized_gross_edge_total = 0
        total_fees_paid = 0
        total_scratch_loss = 0
        net_realized_pnl_total = 0

        # Create simulator instance for baseline config
        baseline_micro_cfg = MicrostructureConfig(
            network_latency_ms=self.config.microstructure_config.network_latency_ms,
            decision_latency_ms=self.config.microstructure_config.decision_latency_ms,
            sequential_leg_delay_ms=self.config.microstructure_config.sequential_leg_delay_ms,
            scratch_timeout_ms=self.config.microstructure_config.scratch_timeout_ms,
            max_scratch_slippage_cents=self.config.microstructure_config.max_scratch_slippage_cents,
            sequencing_policy=self.config.leg_sequencing,
            unwind_policy=self.config.unwind_policy,
            fee_config=self.config.fee_config,
            enable_latency_decay=self.config.microstructure_config.enable_latency_decay,
            latency_decay_threshold_ms=self.config.microstructure_config.latency_decay_threshold_ms,
            latency_decay_rate_per_100ms=self.config.microstructure_config.latency_decay_rate_per_100ms,
            adverse_price_move_threshold_ms=self.config.microstructure_config.adverse_price_move_threshold_ms,
        )
        simulator = RealisticExecutionSimulator(config=baseline_micro_cfg)

        # 3. Chronological replay
        for obs_idx, obs in enumerate(dataset.observations):
            if obs.market:
                latest_markets[obs.ticker] = obs.market
            if obs.order_book:
                latest_books[obs.ticker] = obs.order_book

            book = obs.order_book
            market = obs.market
            if book is None or market is None:
                continue

            # Freshness check
            obs_ts = _normalize_timestamp(obs.observed_at)
            book_ts = _normalize_timestamp(book.source_timestamp) if book.source_timestamp else obs_ts
            age_s = (obs_ts - book_ts).total_seconds()
            if age_s > self.config.max_stale_seconds:
                continue

            # Evaluate binary parity
            parity_opp = evaluate_market_parity(market, quantity=self.config.target_quantity)
            if not parity_opp.is_arbitrage:
                continue

            total_detected += 1
            opp_id = f"V8-BP-{obs.ticker}-{obs_idx}"
            # Point-in-time universe evaluation (strictly prevents look-ahead bias)
            point_in_time_profile = self.universe_selector.profile_market(market, book)
            in_univ = point_in_time_profile.is_liquid
            univ_rejections = point_in_time_profile.rejection_reasons
            latest_profiles[obs.ticker] = point_in_time_profile

            # Evaluate execution pricing under flat vs CFTC fee
            flat_pricing = evaluate_binary_parity_execution(
                book,
                requested_quantity=self.config.target_quantity,
                fee_per_contract_cents=self.config.flat_fee_per_contract_cents,
                fee_model=None,
            )

            cftc_pricing = evaluate_binary_parity_execution(
                book,
                requested_quantity=self.config.target_quantity,
                fee_per_contract_cents=None,
                fee_model=self.config.fee_config,
                series_ticker=obs.ticker,
            )

            is_flat_prof = bool(flat_pricing.is_net_profitable and flat_pricing.supported_quantity > 0)
            is_cftc_prof = bool(cftc_pricing.is_net_profitable and cftc_pricing.supported_quantity > 0)

            if is_flat_prof:
                flat_profitable_count += 1
            if is_cftc_prof:
                cftc_profitable_count += 1

            total_theo_flat_fees += flat_pricing.estimated_fees_cents or 0
            total_theo_cftc_fees += cftc_pricing.estimated_fees_cents or 0

            # Discrepancy checks
            is_reversal = is_flat_prof and not is_cftc_prof
            is_advantage = is_cftc_prof and not is_flat_prof
            if is_reversal:
                fee_reversal_count += 1
            if is_advantage:
                cftc_advantage_count += 1

            # Decide whether to execute
            # Must pass universe check (if enforced) and be profitable under realistic CFTC fee
            should_execute = (not self.config.enforce_universe or in_univ) and is_cftc_prof

            sim_trade: Optional[AsyncMultiLegTrade] = None
            exec_state: Optional[str] = None
            is_complete = False
            filled_qty = 0
            trade_gross_pnl = 0
            trade_fees = 0
            trade_scratch = 0
            trade_net_pnl = 0
            exposure_map: Dict[str, int] = {}

            if should_execute:
                trades_attempted += 1
                target_q = min(cftc_pricing.supported_quantity, self.config.target_quantity)
                target_contracts_total += target_q

                yes_cid = f"{obs.ticker}:YES"
                no_cid = f"{obs.ticker}:NO"
                legs_specs = [
                    (yes_cid, obs.ticker, YES),
                    (no_cid, obs.ticker, NO),
                ]
                yes_asks = derive_ask_levels(book, YES)
                no_asks = derive_ask_levels(book, NO)
                depth_by_leg = {
                    yes_cid: [(lvl.price_cents, lvl.quantity) for lvl in yes_asks],
                    no_cid: [(lvl.price_cents, lvl.quantity) for lvl in no_asks],
                }
                bids_by_leg = {
                    yes_cid: [(dollars_to_cents(lvl.price_dollars), int(lvl.quantity)) for lvl in book.yes_bids],
                    no_cid: [(dollars_to_cents(lvl.price_dollars), int(lvl.quantity)) for lvl in book.no_bids],
                }

                sim_trade = simulator.simulate_execution(
                    trade_id=opp_id,
                    strategy_type=OPPORTUNITY_BINARY_PARITY,
                    target_quantity=target_q,
                    legs_specs=legs_specs,
                    available_depth_by_leg=depth_by_leg,
                    counterparty_bids_by_leg=bids_by_leg,
                    start_time=obs_ts,
                )
                async_trades.append(sim_trade)
                exec_state = sim_trade.state.value
                is_complete = sim_trade.is_arbitrage_complete
                filled_qty = min(l.filled_quantity for l in sim_trade.legs) if sim_trade.legs else 0
                filled_contracts_total += filled_qty

                # Track lifecycle metrics
                if sim_trade.state == MultiLegLifecycleState.FILLED:
                    trades_fully_filled += 1
                elif sim_trade.state in (MultiLegLifecycleState.CLOSED, MultiLegLifecycleState.LEGGED_OUT):
                    trades_legged_out += 1
                elif sim_trade.state == MultiLegLifecycleState.FAILED:
                    trades_failed += 1

                trade_gross_pnl = sim_trade.gross_pnl_cents
                trade_fees = sim_trade.total_fee_cents
                trade_scratch = sim_trade.scratch_loss_cents
                if sim_trade.is_arbitrage_complete:
                    trade_net_pnl = trade_gross_pnl - trade_fees
                else:
                    trade_net_pnl = sim_trade.realized_pnl_cents - trade_fees

                realized_gross_edge_total += trade_gross_pnl
                total_fees_paid += trade_fees
                total_scratch_loss += trade_scratch
                net_realized_pnl_total += trade_net_pnl

                exposure_map = {f"{cid}:{side}": qty for (cid, side), qty in sim_trade.residual_exposure.items()}

                # Reconcile into paper ledger
                apply_async_trade_to_portfolio(portfolio, sim_trade)

            record = V8OpportunityRecord(
                opportunity_id=opp_id,
                observed_at=obs.observed_at,
                strategy_type=OPPORTUNITY_BINARY_PARITY,
                market_tickers=(obs.ticker,),
                in_universe=in_univ,
                universe_rejection_reasons=univ_rejections,
                gross_edge_cents=parity_opp.gross_edge_cents,
                flat_net_edge_cents=flat_pricing.net_profit_cents or 0,
                cftc_net_edge_cents=cftc_pricing.net_profit_cents or 0,
                is_fee_reversal=is_reversal,
                is_cftc_advantage=is_advantage,
                executed=should_execute,
                execution_state=exec_state,
                is_arbitrage_complete=is_complete,
                target_quantity=self.config.target_quantity if should_execute else 0,
                filled_quantity=filled_qty,
                gross_pnl_cents=trade_gross_pnl,
                fees_paid_cents=trade_fees,
                scratch_loss_cents=trade_scratch,
                net_realized_pnl_cents=trade_net_pnl,
                residual_exposure=exposure_map,
                async_trade=sim_trade,
            )
            records.append(record)

        # Construct summary universe selection result from evaluated point-in-time market profiles
        eval_profiles = list(latest_profiles.values())
        eval_profiles.sort(key=lambda p: p.liquidity_score or Decimal("0"), reverse=True)
        if self.universe_selector.config.max_selected_markets is not None:
            selected_profs = [p for p in eval_profiles if p.is_liquid][: self.universe_selector.config.max_selected_markets]
        else:
            selected_profs = [p for p in eval_profiles if p.is_liquid]
        selected_tickers = tuple(p.ticker for p in selected_profs)
        rejections: Dict[str, int] = {}
        for p in eval_profiles:
            if not p.is_liquid:
                for r in p.rejection_reasons:
                    rejections[r] = rejections.get(r, 0) + 1
        universe_result = UniverseSelectionResult(
            selected_tickers=selected_tickers,
            evaluated_markets_count=len(eval_profiles),
            selected_count=len(selected_tickers),
            rejected_count=len(eval_profiles) - len(selected_tickers),
            rejections_by_reason=rejections,
            profiles=tuple(eval_profiles),
            explanation=(
                f"Point-in-time evaluated {len(eval_profiles)} unique markets across replay: "
                f"{len(selected_tickers)} selected as liquid, {len(eval_profiles) - len(selected_tickers)} rejected."
            ),
        )

        # 4. Check paper portfolio reconciliation
        recon = portfolio.reconcile()

        # 5. Calculate drawdown
        total_pos_cost = sum(p.total_cost_cents for p in portfolio.positions.values())
        portfolio_equity = portfolio.total_cash_cents + total_pos_cost
        drawdown_cents = max(0, self.config.initial_cash_cents - portfolio_equity)
        drawdown_pct = _pct(drawdown_cents, self.config.initial_cash_cents)

        # 6. Fee metrics object
        fee_metrics = V8FeeDiscrepancyMetrics(
            total_detected_opportunities=total_detected,
            flat_fee_profitable_count=flat_profitable_count,
            cftc_fee_profitable_count=cftc_profitable_count,
            fee_reversal_count=fee_reversal_count,
            fee_reversal_pct=_pct(fee_reversal_count, total_detected),
            cftc_advantage_count=cftc_advantage_count,
            cftc_advantage_pct=_pct(cftc_advantage_count, total_detected),
            total_theoretical_flat_fees_cents=total_theo_flat_fees,
            total_theoretical_cftc_fees_cents=total_theo_cftc_fees,
        )

        # 7. Run Latency Sensitivity Sweep
        sensitivity_points: List[V8LatencySensitivityPoint] = []
        for lat_ms in self.config.sensitivity_latencies_ms:
            sweep_micro_cfg = MicrostructureConfig(
                network_latency_ms=lat_ms,
                decision_latency_ms=self.config.microstructure_config.decision_latency_ms,
                sequential_leg_delay_ms=self.config.microstructure_config.sequential_leg_delay_ms,
                scratch_timeout_ms=self.config.microstructure_config.scratch_timeout_ms,
                max_scratch_slippage_cents=self.config.microstructure_config.max_scratch_slippage_cents,
                sequencing_policy=self.config.leg_sequencing,
                unwind_policy=self.config.unwind_policy,
                fee_config=self.config.fee_config,
                enable_latency_decay=True,  # Test latency depth decay during sweep
                latency_decay_threshold_ms=max(50.0, lat_ms * 0.8),
                latency_decay_rate_per_100ms=0.25,
                adverse_price_move_threshold_ms=max(200.0, lat_ms * 1.5),
            )
            sweep_sim = RealisticExecutionSimulator(config=sweep_micro_cfg)
            sw_attempted = 0
            sw_filled = 0
            sw_legged = 0
            sw_failed = 0
            sw_net_pnl = 0
            sw_scratch = 0

            # Re-evaluate all executed opportunity candidates under this latency
            for rec in records:
                if not rec.executed or rec.async_trade is None:
                    continue
                orig_trade = rec.async_trade
                sw_attempted += 1

                # Reconstruct legs and books from recorded trade
                legs_specs = [
                    (l.contract_id, l.market_ticker, l.side) for l in orig_trade.legs
                ]
                depth_by_leg = {
                    l.contract_id: [
                        (lvl.price_cents, lvl.quantity) for lvl in l.consumed_levels
                    ]
                    for l in orig_trade.legs
                }
                # Empty counterparty bids fallback if not saved
                bids_by_leg = {l.contract_id: () for l in orig_trade.legs}

                sw_trade = sweep_sim.simulate_execution(
                    trade_id=f"SWEEP-{lat_ms}-{orig_trade.trade_id}",
                    strategy_type=orig_trade.strategy_type,
                    target_quantity=orig_trade.target_quantity,
                    legs_specs=legs_specs,
                    available_depth_by_leg=depth_by_leg,
                    counterparty_bids_by_leg=bids_by_leg,
                    start_time=rec.observed_at,
                )

                if sw_trade.state == MultiLegLifecycleState.FILLED:
                    sw_filled += 1
                elif sw_trade.state in (MultiLegLifecycleState.CLOSED, MultiLegLifecycleState.LEGGED_OUT):
                    sw_legged += 1
                elif sw_trade.state == MultiLegLifecycleState.FAILED:
                    sw_failed += 1

                sw_net_pnl += sw_trade.realized_pnl_cents
                sw_scratch += sw_trade.scratch_loss_cents

            sensitivity_points.append(
                V8LatencySensitivityPoint(
                    latency_ms=lat_ms,
                    trades_attempted=sw_attempted,
                    trades_fully_filled=sw_filled,
                    trades_legged_out=sw_legged,
                    trades_failed=sw_failed,
                    completion_rate_pct=_pct(sw_filled, sw_attempted),
                    legged_out_rate_pct=_pct(sw_legged, sw_attempted),
                    net_realized_pnl_cents=sw_net_pnl,
                    total_scratch_loss_cents=sw_scratch,
                )
            )

        dataset_summary = {
            "source_description": dataset.source_description,
            "total_raw_records": dataset.total_raw_records,
            "valid_observations_count": len(dataset.observations),
            "malformed_records_count": dataset.malformed_records_count,
            "duplicate_records_count": dataset.duplicate_records_count,
            "out_of_order_records_count": dataset.out_of_order_records_count,
            "earliest_timestamp": dataset.earliest_timestamp.isoformat() if dataset.earliest_timestamp else None,
            "latest_timestamp": dataset.latest_timestamp.isoformat() if dataset.latest_timestamp else None,
            "unique_tickers_count": len(dataset.unique_tickers),
        }

        return V8SimulationReport(
            config=self.config,
            dataset_summary=dataset_summary,
            universe_result=universe_result,
            fee_metrics=fee_metrics,
            records=tuple(records),
            trades_attempted=trades_attempted,
            trades_fully_filled=trades_fully_filled,
            trades_legged_out=trades_legged_out,
            trades_failed=trades_failed,
            completion_rate_pct=_pct(trades_fully_filled, trades_attempted),
            legged_out_rate_pct=_pct(trades_legged_out, trades_attempted),
            target_contracts_total=target_contracts_total,
            filled_contracts_total=filled_contracts_total,
            initial_cash_cents=self.config.initial_cash_cents,
            final_cash_cents=portfolio.available_cash_cents,
            final_equity_cents=portfolio_equity,
            realized_gross_edge_cents=realized_gross_edge_total,
            total_exchange_fees_cents=total_fees_paid,
            total_scratch_loss_cents=total_scratch_loss,
            net_realized_pnl_cents=net_realized_pnl_total,
            max_drawdown_cents=drawdown_cents,
            max_drawdown_pct=drawdown_pct,
            ledger_is_reconciled=recon.is_reconciled,
            reconciliation_discrepancies=tuple(recon.discrepancies),
            latency_sensitivity=tuple(sensitivity_points),
            async_trades=tuple(async_trades),
        )
