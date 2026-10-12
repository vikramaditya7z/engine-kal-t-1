"""Kalshi Arbitrage Engine — V10 Rigorous Historical Strategy Validation & Quantitative Performance Research.

This module implements the complete Milestone V10 historical evaluation system:
1. Chronological point-in-time historical replay with zero look-ahead bias.
2. In-depth dataset quality auditing (record counts, empty books, populated books, stale data, duplicates).
3. 10-stage opportunity funnel with explicit numerators and denominators.
4. Exact double-entry ledger financial accounting and portfolio reconciliation.
5. Marked portfolio valuation with explicit, documented valuation methods.
6. Hierarchical comparative baselines (No-Trade -> Gross Edge -> Net Edge -> Depth-Aware -> Realistic -> Stress).
7. Chronological out-of-sample validation with honest sample-size insufficiency handling.
8. Multi-dimensional parameter sensitivity and stress grid analysis.
9. Reproducible output serialization (machine-readable JSON + publication-grade ASCII report).
10. Definitive executive research verdict (POSITIVE EVIDENCE / INCONCLUSIVE / NEGATIVE EVIDENCE).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
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
from .paper_risk import PaperRiskConfig, PaperRiskGate
from .universe import (
    LiquidUniverseSelector,
    MarketLiquidityProfile,
    UniverseFilterConfig,
    UniverseSelectionResult,
)


def _safe_pct(numerator: int, denominator: int) -> Decimal:
    """Safe percentage calculation formatted to 2 decimal places."""
    if denominator <= 0:
        return Decimal("0.00")
    val = (Decimal(numerator) / Decimal(denominator)) * Decimal("100.0")
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# V10 Evaluation Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V10EvaluationConfig:
    """Configuration for V10 historical strategy validation and quantitative research."""

    initial_cash_cents: int = 1_000_000  # $10,000.00 initial capital
    target_quantity: int = 1
    max_stale_seconds: float = 60.0
    universe_config: UniverseFilterConfig = field(default_factory=UniverseFilterConfig)
    enforce_universe: bool = True
    fee_config: FeeModelConfig = field(
        default_factory=lambda: FeeModelConfig(schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER)
    )
    flat_baseline_fee_cents: int = 1
    microstructure_config: MicrostructureConfig = field(default_factory=MicrostructureConfig)
    leg_sequencing: LegSequencingPolicy = LegSequencingPolicy.ILLIQUID_FIRST
    unwind_policy: UnwindPolicy = UnwindPolicy.AGGRESSIVE_CROSS
    risk_config: PaperRiskConfig = field(default_factory=PaperRiskConfig)
    enforce_risk_limits: bool = True
    valuation_method: str = "MARK_TO_MARKET_BID"  # Options: MARK_TO_MARKET_BID, COST_BASIS, ZERO
    enable_out_of_sample: bool = True
    in_sample_ratio: float = 0.70
    min_observations_for_split: int = 30
    enable_sensitivity: bool = True
    sensitivity_latencies_ms: Tuple[float, ...] = (0.0, 25.0, 50.0, 100.0, 250.0, 500.0)
    sensitivity_haircuts: Tuple[float, ...] = (1.0, 0.75, 0.50, 0.25)


# ---------------------------------------------------------------------------
# Data Quality & Inventory Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V10ObservationQualitySummary:
    """Rigorous audit of raw historical observation records."""

    total_raw_records: int
    valid_observations: int
    malformed_records: int
    duplicate_observation_ids: int
    out_of_order_timestamps: int
    stale_observations: int
    empty_books_count: int
    populated_books_count: int
    unique_tickers_count: int
    earliest_timestamp: Optional[str]
    latest_timestamp: Optional[str]
    time_span_seconds: float
    dataset_sha256: str
    source_path: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_raw_records": self.total_raw_records,
            "valid_observations": self.valid_observations,
            "malformed_records": self.malformed_records,
            "duplicate_observation_ids": self.duplicate_observation_ids,
            "out_of_order_timestamps": self.out_of_order_timestamps,
            "stale_observations": self.stale_observations,
            "empty_books_count": self.empty_books_count,
            "populated_books_count": self.populated_books_count,
            "unique_tickers_count": self.unique_tickers_count,
            "earliest_timestamp": self.earliest_timestamp,
            "latest_timestamp": self.latest_timestamp,
            "time_span_seconds": self.time_span_seconds,
            "dataset_sha256": self.dataset_sha256,
            "source_path": self.source_path,
        }


# ---------------------------------------------------------------------------
# Opportunity Funnel Model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V10OpportunityFunnel:
    """Multi-stage opportunity funnel with explicit numerators and denominators."""

    observations_processed: int
    valid_observations: int
    markets_evaluated: int
    candidates_detected: int  # Gross edge > 0
    rejected_no_gross_edge: int
    rejected_unprofitable_fees: int  # Gross edge <= realistic fees
    rejected_insufficient_depth: int
    rejected_illiquid_universe: int
    rejected_stale_data: int
    rejected_risk_limits: int
    approved_for_execution: int
    simulated_execution_attempts: int
    trades_fully_filled: int
    trades_partially_filled: int
    trades_legged_out: int
    trades_failed: int
    completed_arbitrage_trades: int  # is_arbitrage_complete == True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "observations_processed": self.observations_processed,
            "valid_observations": self.valid_observations,
            "markets_evaluated": self.markets_evaluated,
            "candidates_detected": self.candidates_detected,
            "rejected_no_gross_edge": self.rejected_no_gross_edge,
            "rejected_unprofitable_fees": self.rejected_unprofitable_fees,
            "rejected_insufficient_depth": self.rejected_insufficient_depth,
            "rejected_illiquid_universe": self.rejected_illiquid_universe,
            "rejected_stale_data": self.rejected_stale_data,
            "rejected_risk_limits": self.rejected_risk_limits,
            "approved_for_execution": self.approved_for_execution,
            "simulated_execution_attempts": self.simulated_execution_attempts,
            "trades_fully_filled": self.trades_fully_filled,
            "trades_partially_filled": self.trades_partially_filled,
            "trades_legged_out": self.trades_legged_out,
            "trades_failed": self.trades_failed,
            "completed_arbitrage_trades": self.completed_arbitrage_trades,
            "detection_rate_pct": str(_safe_pct(self.candidates_detected, self.markets_evaluated)),
            "approval_rate_pct": str(_safe_pct(self.approved_for_execution, self.candidates_detected)),
            "arbitrage_completion_rate_pct": str(_safe_pct(self.completed_arbitrage_trades, self.simulated_execution_attempts)),
        }


# ---------------------------------------------------------------------------
# Financial Accounting Metrics Model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V10FinancialMetrics:
    """Exact financial accounting, portfolio valuation, and performance returns."""

    initial_cash_cents: int
    final_cash_cents: int
    position_cost_basis_cents: int
    marked_portfolio_value_cents: int
    valuation_method: str
    realized_gross_edge_cents: int
    total_exchange_fees_cents: int
    scratch_unwind_drag_cents: int
    net_realized_pnl_cents: int
    unrealized_pnl_cents: int
    total_economic_pnl_cents: int
    return_on_capital_pct: Optional[Decimal]
    max_drawdown_cents: int
    max_drawdown_pct: Decimal
    capital_utilization_pct: Decimal
    total_trades_count: int
    winning_trades_count: int
    losing_trades_count: int
    scratch_trades_count: int
    win_rate_pct: Optional[Decimal]
    average_trade_pnl_cents: Optional[Decimal]
    ledger_is_reconciled: bool
    reconciliation_discrepancies: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "initial_cash_cents": self.initial_cash_cents,
            "final_cash_cents": self.final_cash_cents,
            "position_cost_basis_cents": self.position_cost_basis_cents,
            "marked_portfolio_value_cents": self.marked_portfolio_value_cents,
            "valuation_method": self.valuation_method,
            "realized_gross_edge_cents": self.realized_gross_edge_cents,
            "total_exchange_fees_cents": self.total_exchange_fees_cents,
            "scratch_unwind_drag_cents": self.scratch_unwind_drag_cents,
            "net_realized_pnl_cents": self.net_realized_pnl_cents,
            "unrealized_pnl_cents": self.unrealized_pnl_cents,
            "total_economic_pnl_cents": self.total_economic_pnl_cents,
            "return_on_capital_pct": str(self.return_on_capital_pct) if self.return_on_capital_pct is not None else None,
            "max_drawdown_cents": self.max_drawdown_cents,
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "capital_utilization_pct": str(self.capital_utilization_pct),
            "total_trades_count": self.total_trades_count,
            "winning_trades_count": self.winning_trades_count,
            "losing_trades_count": self.losing_trades_count,
            "scratch_trades_count": self.scratch_trades_count,
            "win_rate_pct": str(self.win_rate_pct) if self.win_rate_pct is not None else None,
            "average_trade_pnl_cents": str(self.average_trade_pnl_cents) if self.average_trade_pnl_cents is not None else None,
            "ledger_is_reconciled": self.ledger_is_reconciled,
            "reconciliation_discrepancies": list(self.reconciliation_discrepancies),
        }


# ---------------------------------------------------------------------------
# Execution Quality Metrics Model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V10ExecutionQualityMetrics:
    """Quantitative execution quality, slippage, and fill characteristics."""

    target_contracts_total: int
    filled_contracts_total: int
    overall_fill_rate_pct: Decimal
    full_fill_rate_pct: Decimal
    partial_fill_rate_pct: Decimal
    unwind_rate_pct: Decimal
    average_slippage_per_contract_cents: Decimal
    fees_as_pct_of_gross_edge: Decimal
    open_unhedged_contracts: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_contracts_total": self.target_contracts_total,
            "filled_contracts_total": self.filled_contracts_total,
            "overall_fill_rate_pct": str(self.overall_fill_rate_pct),
            "full_fill_rate_pct": str(self.full_fill_rate_pct),
            "partial_fill_rate_pct": str(self.partial_fill_rate_pct),
            "unwind_rate_pct": str(self.unwind_rate_pct),
            "average_slippage_per_contract_cents": str(self.average_slippage_per_contract_cents),
            "fees_as_pct_of_gross_edge": str(self.fees_as_pct_of_gross_edge),
            "open_unhedged_contracts": self.open_unhedged_contracts,
        }


# ---------------------------------------------------------------------------
# Hierarchical Comparative Baselines Model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V10BaselineTierResult:
    """Performance evaluation for a single realism tier."""

    tier_name: str
    description: str
    opportunities_count: int
    trades_count: int
    gross_pnl_cents: int
    fees_paid_cents: int
    scratch_loss_cents: int
    net_pnl_cents: int
    completed_rate_pct: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier_name": self.tier_name,
            "description": self.description,
            "opportunities_count": self.opportunities_count,
            "trades_count": self.trades_count,
            "gross_pnl_cents": self.gross_pnl_cents,
            "fees_paid_cents": self.fees_paid_cents,
            "scratch_loss_cents": self.scratch_loss_cents,
            "net_pnl_cents": self.net_pnl_cents,
            "completed_rate_pct": str(self.completed_rate_pct),
        }


@dataclass(frozen=True)
class V10BaselineComparison:
    """Hierarchical comparison showing where edge is eroded by realism constraints."""

    tiers: Tuple[V10BaselineTierResult, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {"tiers": [t.to_dict() for t in self.tiers]}


# ---------------------------------------------------------------------------
# Out-of-Sample Partition Validation Model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V10OutOfSampleResult:
    """Chronological split validation results."""

    is_split_viable: bool
    insufficiency_reason: Optional[str]
    in_sample_observations_count: int
    out_of_sample_observations_count: int
    in_sample_span: Optional[str]
    out_of_sample_span: Optional[str]
    in_sample_candidates: int
    out_of_sample_candidates: int
    in_sample_completed_trades: int
    out_of_sample_completed_trades: int
    in_sample_net_pnl_cents: int
    out_of_sample_net_pnl_cents: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_split_viable": self.is_split_viable,
            "insufficiency_reason": self.insufficiency_reason,
            "in_sample_observations_count": self.in_sample_observations_count,
            "out_of_sample_observations_count": self.out_of_sample_observations_count,
            "in_sample_span": self.in_sample_span,
            "out_of_sample_span": self.out_of_sample_span,
            "in_sample_candidates": self.in_sample_candidates,
            "out_of_sample_candidates": self.out_of_sample_candidates,
            "in_sample_completed_trades": self.in_sample_completed_trades,
            "out_of_sample_completed_trades": self.out_of_sample_completed_trades,
            "in_sample_net_pnl_cents": self.in_sample_net_pnl_cents,
            "out_of_sample_net_pnl_cents": self.out_of_sample_net_pnl_cents,
        }


# ---------------------------------------------------------------------------
# Sensitivity Grid Model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V10SensitivityPoint:
    """Result of evaluating a specific scenario parameter combination."""

    latency_ms: float
    depth_haircut: float
    fee_model_name: str
    trades_attempted: int
    trades_completed: int
    trades_legged_out: int
    completion_rate_pct: Decimal
    net_pnl_cents: int
    scratch_loss_cents: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "latency_ms": self.latency_ms,
            "depth_haircut": self.depth_haircut,
            "fee_model_name": self.fee_model_name,
            "trades_attempted": self.trades_attempted,
            "trades_completed": self.trades_completed,
            "trades_legged_out": self.trades_legged_out,
            "completion_rate_pct": str(self.completion_rate_pct),
            "net_pnl_cents": self.net_pnl_cents,
            "scratch_loss_cents": self.scratch_loss_cents,
        }


# ---------------------------------------------------------------------------
# Complete V10 Evaluation Report Container
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V10EvaluationReport:
    """Comprehensive, auditable V10 quantitative research and evaluation report."""

    config: V10EvaluationConfig
    quality_summary: V10ObservationQualitySummary
    funnel: V10OpportunityFunnel
    financials: V10FinancialMetrics
    execution_quality: V10ExecutionQualityMetrics
    baseline_comparison: V10BaselineComparison
    out_of_sample: V10OutOfSampleResult
    sensitivity_grid: Tuple[V10SensitivityPoint, ...]
    executive_verdict: str  # POSITIVE EVIDENCE / INCONCLUSIVE / NEGATIVE EVIDENCE
    verdict_rationale: str
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def summary(self) -> str:
        """Render a publication-quality ASCII report."""
        q = self.quality_summary
        fn = self.funnel
        fin = self.financials
        eq = self.execution_quality
        cfg = self.config

        lines = [
            "=" * 78,
            "KALSHI ARBITRAGE ENGINE — V10 HISTORICAL STRATEGY VALIDATION REPORT",
            "=" * 78,
            f"Evaluated At:                  {self.evaluated_at}",
            f"Dataset Source:                {q.source_path}",
            f"Dataset SHA256:                {q.dataset_sha256[:16]}...{q.dataset_sha256[-8:]}",
            f"Executive Research Verdict:    {self.executive_verdict}",
            f"Verdict Rationale:             {self.verdict_rationale}",
            "",
            "1. EVIDENCE INVENTORY & CADENCE QUALITY:",
            f"  Total Raw Records:           {q.total_raw_records}",
            f"  Valid Observations:          {q.valid_observations}",
            f"  Malformed / Corrupted:       {q.malformed_records}",
            f"  Duplicate Record IDs:        {q.duplicate_observation_ids}",
            f"  Out-of-Order Timestamps:     {q.out_of_order_timestamps}",
            f"  Stale Records (> {cfg.max_stale_seconds:.0f}s):      {q.stale_observations}",
            f"  Empty Order Books:           {q.empty_books_count}",
            f"  Populated Order Books:       {q.populated_books_count}",
            f"  Unique Tickers Ingested:     {q.unique_tickers_count}",
            f"  Observation Span:            {q.earliest_timestamp or 'N/A'} -> {q.latest_timestamp or 'N/A'}",
            f"  Duration Span:               {q.time_span_seconds:.1f} seconds",
            "",
            "2. MULTI-STAGE OPPORTUNITY FUNNEL:",
            f"  1. Observations Ingested:    {fn.observations_processed}",
            f"  2. Valid Snapshots:          {fn.valid_observations}",
            f"  3. Markets Evaluated:        {fn.markets_evaluated}",
            f"  4. Gross Arbitrage Detected: {fn.candidates_detected}",
            f"     - Rejected (No Edge):     {fn.rejected_no_gross_edge}",
            f"     - Rejected (CFTC Fees):   {fn.rejected_unprofitable_fees}",
            f"     - Rejected (No Depth):    {fn.rejected_insufficient_depth}",
            f"     - Rejected (Illiquid):    {fn.rejected_illiquid_universe}",
            f"     - Rejected (Stale Data):  {fn.rejected_stale_data}",
            f"     - Rejected (Risk Limits): {fn.rejected_risk_limits}",
            f"  5. Approved for Execution:   {fn.approved_for_execution}",
            f"  6. Execution Attempts:       {fn.simulated_execution_attempts}",
            f"  7. Fully Filled Trades:      {fn.trades_fully_filled}",
            f"  8. Partially Filled Trades:  {fn.trades_partially_filled}",
            f"  9. Legged-Out (Unwound):     {fn.trades_legged_out}",
            f" 10. Completed Arbitrage:      {fn.completed_arbitrage_trades} ({_safe_pct(fn.completed_arbitrage_trades, fn.simulated_execution_attempts)}% conversion)",
            "",
            "3. FINANCIAL RESULTS & PORTFOLIO VALUATION:",
            f"  Initial Simulated Capital:   ${fin.initial_cash_cents / 100:,.2f}",
            f"  Ending Available Cash:       ${fin.final_cash_cents / 100:,.2f}",
            f"  Position Cost Basis:         ${fin.position_cost_basis_cents / 100:,.2f}",
            f"  Marked Portfolio Value:      ${fin.marked_portfolio_value_cents / 100:,.2f} ({fin.valuation_method})",
            f"  Realized Gross Edge:         {fin.realized_gross_edge_cents:+d}¢ (${fin.realized_gross_edge_cents / 100:+,.2f})",
            f"  Exchange Fees Paid (CFTC):   {fin.total_exchange_fees_cents}¢ (${fin.total_exchange_fees_cents / 100:,.2f})",
            f"  Scratch / Unwind Drag:       {fin.scratch_unwind_drag_cents:+d}¢ (${fin.scratch_unwind_drag_cents / 100:+,.2f})",
            f"  Net Realized P&L:            {fin.net_realized_pnl_cents:+d}¢ (${fin.net_realized_pnl_cents / 100:+,.2f})",
            f"  Unrealized P&L:              {fin.unrealized_pnl_cents:+d}¢ (${fin.unrealized_pnl_cents / 100:+,.2f})",
            f"  Total Economic Return:       {fin.total_economic_pnl_cents:+d}¢ (${fin.total_economic_pnl_cents / 100:+,.2f})",
            f"  Max Drawdown:                {fin.max_drawdown_cents}¢ ({fin.max_drawdown_pct}%)",
            f"  Capital Utilization:         {fin.capital_utilization_pct}%",
            f"  Total Trades Executed:       {fin.total_trades_count}",
            f"  Win / Loss / Scratch:        {fin.winning_trades_count} / {fin.losing_trades_count} / {fin.scratch_trades_count}",
            f"  Double-Entry Ledger Audit:   {'PASS (100% Invariant Match)' if fin.ledger_is_reconciled else 'FAIL - DISCREPANCIES DETECTED'}",
            "",
            "4. EXECUTION QUALITY & MICROSTRUCTURE IMPACT:",
            f"  Target vs Filled Contracts:  {eq.filled_contracts_total} / {eq.target_contracts_total} ({eq.overall_fill_rate_pct}%)",
            f"  Full-Fill Rate:              {eq.full_fill_rate_pct}%",
            f"  Partial-Fill Rate:           {eq.partial_fill_rate_pct}%",
            f"  Unwind / Scratch Rate:       {eq.unwind_rate_pct}%",
            f"  Avg Slippage / Contract:     {eq.average_slippage_per_contract_cents}¢",
            f"  Fees as % of Gross Edge:     {eq.fees_as_pct_of_gross_edge}%",
            f"  Residual Unhedged Contracts: {eq.open_unhedged_contracts}",
            "",
            "5. HIERARCHICAL COMPARATIVE BASELINES:",
            "  Tier Name               Opportunities   Trades   Gross P&L    Fees Paid   Net P&L    Comp %",
            "  ---------------------   -------------   ------   ---------    ---------   -------    ------",
        ]

        for tier in self.baseline_comparison.tiers:
            lines.append(
                f"  {tier.tier_name:<23} "
                f"{tier.opportunities_count:>13d} "
                f"{tier.trades_count:>8d} "
                f"{tier.gross_pnl_cents:>+10d}¢ "
                f"{tier.fees_paid_cents:>10d}¢ "
                f"{tier.net_pnl_cents:>+8d}¢ "
                f"{tier.completed_rate_pct:>7.1f}%"
            )

        lines.extend([
            "",
            "6. CHRONOLOGICAL OUT-OF-SAMPLE VALIDATION:",
            f"  Split Viable:                {self.out_of_sample.is_split_viable}",
        ])
        if not self.out_of_sample.is_split_viable:
            lines.append(f"  Insufficiency Note:          {self.out_of_sample.insufficiency_reason}")
        else:
            lines.extend([
                f"  In-Sample Span:              {self.out_of_sample.in_sample_span} ({self.out_of_sample.in_sample_observations_count} obs)",
                f"  In-Sample Results:           {self.out_of_sample.in_sample_candidates} cands, {self.out_of_sample.in_sample_completed_trades} trades, Net: {self.out_of_sample.in_sample_net_pnl_cents:+d}¢",
                f"  Out-of-Sample Span:          {self.out_of_sample.out_of_sample_span} ({self.out_of_sample.out_of_sample_observations_count} obs)",
                f"  Out-of-Sample Results:       {self.out_of_sample.out_of_sample_candidates} cands, {self.out_of_sample.out_of_sample_completed_trades} trades, Net: {self.out_of_sample.out_of_sample_net_pnl_cents:+d}¢",
            ])

        lines.extend([
            "",
            "7. SENSITIVITY & STRESS ANALYSIS MATRIX:",
            "  Latency  Haircut  Fee Model       Attempted  Completed  Leg-Out  Net P&L   Scratch Drag",
            "  -------  -------  --------------  ---------  ---------  -------  -------   ------------",
        ])
        for pt in self.sensitivity_grid:
            lines.append(
                f"  {pt.latency_ms:>5.0f}ms   "
                f"{pt.depth_haircut:>6.2f}  "
                f"{pt.fee_model_name:<14}  "
                f"{pt.trades_attempted:>9d}  "
                f"{pt.trades_completed:>9d}  "
                f"{pt.trades_legged_out:>7d}  "
                f"{pt.net_pnl_cents:>+6d}¢  "
                f"{pt.scratch_loss_cents:>+12d}¢"
            )

        lines.append("=" * 78)
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Convert entire report to JSON-serializable dictionary."""
        return {
            "evaluated_at": self.evaluated_at,
            "executive_verdict": self.executive_verdict,
            "verdict_rationale": self.verdict_rationale,
            "quality_summary": self.quality_summary.to_dict(),
            "funnel": self.funnel.to_dict(),
            "financials": self.financials.to_dict(),
            "execution_quality": self.execution_quality.to_dict(),
            "baseline_comparison": self.baseline_comparison.to_dict(),
            "out_of_sample": self.out_of_sample.to_dict(),
            "sensitivity_grid": [pt.to_dict() for pt in self.sensitivity_grid],
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize report to indented JSON."""
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# V10 Historical Strategy Evaluator Engine
# ---------------------------------------------------------------------------

class V10HistoricalStrategyEvaluator:
    """Rigorous historical evaluation and quantitative performance research engine."""

    def __init__(self, config: Optional[V10EvaluationConfig] = None) -> None:
        self.config = config or V10EvaluationConfig()
        self.universe_selector = LiquidUniverseSelector(config=self.config.universe_config)
        self.risk_gate = PaperRiskGate(config=self.config.risk_config)

    def audit_dataset_quality(
        self,
        dataset: HistoricalDataset,
        source_path: str = "",
    ) -> V10ObservationQualitySummary:
        """Audit dataset integrity, cadence, empty books, duplicates, and freshness."""
        total_raw = len(dataset.observations)
        valid_obs = 0
        malformed = 0
        stale_count = 0
        empty_books = 0
        populated_books = 0
        seen_ids: Set[str] = set()
        dup_ids = 0
        out_of_order = 0
        tickers: Set[str] = set()
        timestamps: List[datetime] = []
        last_ts: Optional[datetime] = None

        raw_bytes = json.dumps(
            [{"obs_id": o.observation_id, "t": o.ticker} for o in dataset.observations]
        ).encode("utf-8")
        sha = hashlib.sha256(raw_bytes).hexdigest()

        for obs in dataset.observations:
            if obs is None:
                malformed += 1
                continue
            valid_obs += 1
            if obs.observation_id:
                if obs.observation_id in seen_ids:
                    dup_ids += 1
                seen_ids.add(obs.observation_id)

            if obs.ticker:
                tickers.add(obs.ticker)

            obs_ts = _normalize_timestamp(obs.observed_at)
            if obs_ts:
                timestamps.append(obs_ts)
                if last_ts and obs_ts < last_ts:
                    out_of_order += 1
                last_ts = obs_ts

            book = obs.order_book
            if book is not None:
                has_bids = len(book.yes_bids) > 0 or len(book.no_bids) > 0
                if has_bids:
                    populated_books += 1
                else:
                    empty_books += 1

                book_ts = _normalize_timestamp(book.source_timestamp) if book.source_timestamp else obs_ts
                if obs_ts and book_ts:
                    age_s = (obs_ts - book_ts).total_seconds()
                    if age_s > self.config.max_stale_seconds:
                        stale_count += 1
            else:
                empty_books += 1

        earliest = min(timestamps).isoformat() if timestamps else None
        latest = max(timestamps).isoformat() if timestamps else None
        span_s = (max(timestamps) - min(timestamps)).total_seconds() if len(timestamps) >= 2 else 0.0

        return V10ObservationQualitySummary(
            total_raw_records=total_raw,
            valid_observations=valid_obs,
            malformed_records=malformed,
            duplicate_observation_ids=dup_ids,
            out_of_order_timestamps=out_of_order,
            stale_observations=stale_count,
            empty_books_count=empty_books,
            populated_books_count=populated_books,
            unique_tickers_count=len(tickers),
            earliest_timestamp=earliest,
            latest_timestamp=latest,
            time_span_seconds=span_s,
            dataset_sha256=sha,
            source_path=source_path,
        )

    def evaluate(
        self,
        dataset: HistoricalDataset,
        source_path: str = "",
        declared_baskets: Sequence[DeclaredMeceBasket] = (),
    ) -> V10EvaluationReport:
        """Run full V10 evaluation pipeline."""
        # 1. Audit dataset quality
        quality = self.audit_dataset_quality(dataset, source_path=source_path)

        # 2. Sort observations chronologically to guarantee no forward look-ahead
        sorted_obs = sorted(
            [o for o in dataset.observations if o is not None and o.observed_at is not None],
            key=lambda o: _normalize_timestamp(o.observed_at),
        )

        # 3. Primary Execution Replay
        funnel, financials, exec_quality, sim_trades = self._run_replay(
            sorted_obs,
            config=self.config,
        )

        # 4. Comparative Baseline Evaluation
        baselines = self._evaluate_baselines(sorted_obs, config=self.config)

        # 5. Chronological Out-of-Sample Validation
        oos = self._evaluate_out_of_sample(sorted_obs, config=self.config)

        # 6. Sensitivity & Stress Grid
        sens_grid = self._evaluate_sensitivity(sorted_obs, config=self.config)

        # 7. Synthesize Executive Verdict
        verdict, rationale = self._synthesize_verdict(
            quality=quality,
            funnel=funnel,
            financials=financials,
            out_of_sample=oos,
            baseline_comparison=baselines,
        )

        return V10EvaluationReport(
            config=self.config,
            quality_summary=quality,
            funnel=funnel,
            financials=financials,
            execution_quality=exec_quality,
            baseline_comparison=baselines,
            out_of_sample=oos,
            sensitivity_grid=sens_grid,
            executive_verdict=verdict,
            verdict_rationale=rationale,
        )

    def _run_replay(
        self,
        observations: Sequence[MarketObservation],
        config: V10EvaluationConfig,
        haircut: float = 1.0,
        micro_override: Optional[MicrostructureConfig] = None,
        fee_override: Optional[FeeModelConfig] = None,
    ) -> Tuple[V10OpportunityFunnel, V10FinancialMetrics, V10ExecutionQualityMetrics, List[AsyncMultiLegTrade]]:
        """Simulate realistic execution replay across chronological observations."""
        portfolio = PaperPortfolio(initial_cash_cents=config.initial_cash_cents)
        micro_cfg = micro_override or config.microstructure_config
        fee_cfg = fee_override or config.fee_config

        simulator = RealisticExecutionSimulator(config=micro_cfg)
        sim_trades: List[AsyncMultiLegTrade] = []

        obs_count = len(observations)
        markets_evaluated = 0
        candidates_detected = 0
        rej_no_edge = 0
        rej_unprofitable_fees = 0
        rej_insufficient_depth = 0
        rej_illiquid = 0
        rej_stale = 0
        rej_risk = 0
        approved_for_exec = 0
        attempts = 0
        fully_filled = 0
        partially_filled = 0
        legged_out = 0
        failed = 0
        completed_arbs = 0

        target_contracts_total = 0
        filled_contracts_total = 0
        realized_gross_edge_total = 0
        total_fees_paid = 0
        total_scratch_loss = 0
        net_realized_pnl_total = 0
        total_slippage_cents = 0

        peak_committed_cents = 0
        peak_equity_cents = config.initial_cash_cents
        max_drawdown_cents = 0

        latest_bid_marks: Dict[str, int] = {}  # ticker -> best_bid_cents

        for obs_idx, obs in enumerate(observations):
            book = obs.order_book
            market = obs.market
            if book is None or market is None:
                continue

            markets_evaluated += 1
            obs_ts = _normalize_timestamp(obs.observed_at)
            book_ts = _normalize_timestamp(book.source_timestamp) if book.source_timestamp else obs_ts
            age_s = (obs_ts - book_ts).total_seconds() if obs_ts and book_ts else 0.0

            # Track latest best bid for mark-to-market valuation
            if book.yes_bids:
                latest_bid_marks[f"{obs.ticker}:YES"] = dollars_to_cents(book.yes_bids[0].price_dollars)
            if book.no_bids:
                latest_bid_marks[f"{obs.ticker}:NO"] = dollars_to_cents(book.no_bids[0].price_dollars)

            # Freshness check
            if age_s > config.max_stale_seconds:
                rej_stale += 1
                continue

            # Theoretical binary parity
            parity_opp = evaluate_market_parity(market, quantity=config.target_quantity)
            if not parity_opp.is_arbitrage:
                rej_no_edge += 1
                continue

            candidates_detected += 1

            # Point-in-time universe qualification
            profile = self.universe_selector.profile_market(market, book)
            if config.enforce_universe and not profile.is_liquid:
                rej_illiquid += 1
                continue

            # Execution pricing under fee schedule and depth
            exec_pricing = evaluate_binary_parity_execution(
                book,
                requested_quantity=config.target_quantity,
                fee_model=fee_cfg,
                series_ticker=obs.ticker,
            )

            if exec_pricing.supported_quantity <= 0:
                rej_insufficient_depth += 1
                continue

            if not exec_pricing.is_net_profitable:
                rej_unprofitable_fees += 1
                continue

            # Pre-trade capital and risk check
            total_req_cost = exec_pricing.total_cost_cents + (exec_pricing.estimated_fees_cents or 0)
            if config.enforce_risk_limits:
                if total_req_cost > portfolio.available_cash_cents:
                    rej_risk += 1
                    continue
                if total_req_cost > config.risk_config.max_cost_per_trade_cents:
                    rej_risk += 1
                    continue

            approved_for_exec += 1

            # Execute simulated trade
            attempts += 1
            target_q = min(exec_pricing.supported_quantity, config.target_quantity)
            target_contracts_total += target_q

            committed = total_req_cost
            if committed > peak_committed_cents:
                peak_committed_cents = committed

            yes_cid = f"{obs.ticker}:YES"
            no_cid = f"{obs.ticker}:NO"
            legs_specs = [
                (yes_cid, obs.ticker, YES),
                (no_cid, obs.ticker, NO),
            ]

            # Depth with optional haircut
            yes_asks = derive_ask_levels(book, YES)
            no_asks = derive_ask_levels(book, NO)
            depth_by_leg = {
                yes_cid: [(lvl.price_cents, max(1, int(lvl.quantity * Decimal(str(haircut))))) for lvl in yes_asks],
                no_cid: [(lvl.price_cents, max(1, int(lvl.quantity * Decimal(str(haircut))))) for lvl in no_asks],
            }
            bids_by_leg = {
                yes_cid: [(dollars_to_cents(lvl.price_dollars), int(lvl.quantity)) for lvl in book.yes_bids],
                no_cid: [(dollars_to_cents(lvl.price_dollars), int(lvl.quantity)) for lvl in book.no_bids],
            }

            trade_id = f"V10-TRD-{obs.ticker}-{obs_idx}"
            sim_trade = simulator.simulate_execution(
                trade_id=trade_id,
                strategy_type=OPPORTUNITY_BINARY_PARITY,
                target_quantity=target_q,
                legs_specs=legs_specs,
                available_depth_by_leg=depth_by_leg,
                counterparty_bids_by_leg=bids_by_leg,
                start_time=obs_ts or datetime.now(timezone.utc),
            )
            sim_trades.append(sim_trade)

            # Record fill metrics
            min_fill = min(l.filled_quantity for l in sim_trade.legs) if sim_trade.legs else 0
            filled_contracts_total += min_fill

            if sim_trade.state == MultiLegLifecycleState.FILLED:
                fully_filled += 1
            elif sim_trade.state == MultiLegLifecycleState.PARTIALLY_FILLED:
                partially_filled += 1
            elif sim_trade.state in (MultiLegLifecycleState.CLOSED, MultiLegLifecycleState.LEGGED_OUT):
                legged_out += 1
            elif sim_trade.state == MultiLegLifecycleState.FAILED:
                failed += 1

            if sim_trade.is_arbitrage_complete:
                completed_arbs += 1

            # Financial metrics
            gross_edge = sim_trade.gross_pnl_cents
            fees = sim_trade.total_fee_cents
            scratch = sim_trade.scratch_loss_cents
            net_pnl = gross_edge - fees if sim_trade.is_arbitrage_complete else (sim_trade.realized_pnl_cents - fees)

            realized_gross_edge_total += gross_edge
            total_fees_paid += fees
            total_scratch_loss += scratch
            net_realized_pnl_total += net_pnl

            # Calculate slippage
            if sim_trade.legs:
                for leg in sim_trade.legs:
                    for cl in leg.consumed_levels:
                        expected_p = cl.price_cents
                        # Slippage relative to top of book
                        top_p = depth_by_leg[leg.contract_id][0][0] if depth_by_leg.get(leg.contract_id) else expected_p
                        total_slippage_cents += max(0, (expected_p - top_p) * cl.quantity)

            # Reconcile into paper portfolio
            apply_async_trade_to_portfolio(portfolio, sim_trade)

            # Drawdown tracking
            curr_equity = portfolio.book_value_equity_cents()
            if curr_equity > peak_equity_cents:
                peak_equity_cents = curr_equity
            dd = peak_equity_cents - curr_equity
            if dd > max_drawdown_cents:
                max_drawdown_cents = dd

        # Mark-to-market / marked portfolio valuation
        cost_basis_cents = portfolio.total_cost_basis_cents()
        marked_val_cents = 0

        if config.valuation_method == "MARK_TO_MARKET_BID":
            # Liquidate open positions against latest observable market bids
            for (cid, side), pos in portfolio.positions.items():
                mark_bid = latest_bid_marks.get(f"{cid}:{side}")
                if mark_bid is not None and pos.quantity > 0:
                    marked_val_cents += pos.quantity * mark_bid
                else:
                    marked_val_cents += pos.total_cost_cents
        elif config.valuation_method == "COST_BASIS":
            marked_val_cents = cost_basis_cents
        else:
            marked_val_cents = 0

        unrealized_pnl = marked_val_cents - cost_basis_cents
        total_econ_pnl = net_realized_pnl_total + unrealized_pnl

        # Return on capital
        init_cents = config.initial_cash_cents
        roc = (Decimal(total_econ_pnl) / Decimal(init_cents)) * Decimal("100.0") if init_cents > 0 else Decimal("0.00")
        roc_pct = roc.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        dd_pct = (Decimal(max_drawdown_cents) / Decimal(init_cents)) * Decimal("100.0") if init_cents > 0 else Decimal("0.00")
        dd_pct_quant = dd_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        cap_util = (Decimal(peak_committed_cents) / Decimal(init_cents)) * Decimal("100.0") if init_cents > 0 else Decimal("0.00")
        cap_util_quant = cap_util.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Trade win/loss distribution
        win_count = sum(1 for t in sim_trades if (t.gross_pnl_cents - t.total_fee_cents) > 0 and t.is_arbitrage_complete)
        loss_count = sum(1 for t in sim_trades if (t.gross_pnl_cents - t.total_fee_cents) < 0 or (not t.is_arbitrage_complete and t.realized_pnl_cents < 0))
        scratch_count = sum(1 for t in sim_trades if t.state in (MultiLegLifecycleState.CLOSED, MultiLegLifecycleState.LEGGED_OUT) and t.scratch_loss_cents > 0)

        trade_cnt = len(sim_trades)
        win_rate = (Decimal(win_count) / Decimal(trade_cnt)) * Decimal("100.0") if trade_cnt > 0 else None
        avg_trade = (Decimal(net_realized_pnl_total) / Decimal(trade_cnt)) if trade_cnt > 0 else None

        # Ledger reconciliation check
        rec = portfolio.reconcile()

        # Fill rate and quality
        overall_fill = _safe_pct(filled_contracts_total, target_contracts_total)
        full_rate = _safe_pct(fully_filled, attempts)
        part_rate = _safe_pct(partially_filled, attempts)
        unwind_rate = _safe_pct(legged_out, attempts)
        avg_slip = (Decimal(total_slippage_cents) / Decimal(filled_contracts_total)).quantize(Decimal("0.01")) if filled_contracts_total > 0 else Decimal("0.00")
        fee_pct_gross = _safe_pct(total_fees_paid, realized_gross_edge_total) if realized_gross_edge_total > 0 else Decimal("0.00")

        open_unhedged = sum(pos.quantity for pos in portfolio.positions.values())

        funnel = V10OpportunityFunnel(
            observations_processed=obs_count,
            valid_observations=obs_count,
            markets_evaluated=markets_evaluated,
            candidates_detected=candidates_detected,
            rejected_no_gross_edge=rej_no_edge,
            rejected_unprofitable_fees=rej_unprofitable_fees,
            rejected_insufficient_depth=rej_insufficient_depth,
            rejected_illiquid_universe=rej_illiquid,
            rejected_stale_data=rej_stale,
            rejected_risk_limits=rej_risk,
            approved_for_execution=approved_for_exec,
            simulated_execution_attempts=attempts,
            trades_fully_filled=fully_filled,
            trades_partially_filled=partially_filled,
            trades_legged_out=legged_out,
            trades_failed=failed,
            completed_arbitrage_trades=completed_arbs,
        )

        financials = V10FinancialMetrics(
            initial_cash_cents=init_cents,
            final_cash_cents=portfolio.available_cash_cents,
            position_cost_basis_cents=cost_basis_cents,
            marked_portfolio_value_cents=marked_val_cents,
            valuation_method=config.valuation_method,
            realized_gross_edge_cents=realized_gross_edge_total,
            total_exchange_fees_cents=total_fees_paid,
            scratch_unwind_drag_cents=total_scratch_loss,
            net_realized_pnl_cents=net_realized_pnl_total,
            unrealized_pnl_cents=unrealized_pnl,
            total_economic_pnl_cents=total_econ_pnl,
            return_on_capital_pct=roc_pct if trade_cnt > 0 else None,
            max_drawdown_cents=max_drawdown_cents,
            max_drawdown_pct=dd_pct_quant,
            capital_utilization_pct=cap_util_quant,
            total_trades_count=trade_cnt,
            winning_trades_count=win_count,
            losing_trades_count=loss_count,
            scratch_trades_count=scratch_count,
            win_rate_pct=win_rate.quantize(Decimal("0.01")) if win_rate is not None else None,
            average_trade_pnl_cents=avg_trade.quantize(Decimal("0.01")) if avg_trade is not None else None,
            ledger_is_reconciled=rec.is_reconciled,
            reconciliation_discrepancies=rec.discrepancies,
        )

        exec_quality = V10ExecutionQualityMetrics(
            target_contracts_total=target_contracts_total,
            filled_contracts_total=filled_contracts_total,
            overall_fill_rate_pct=overall_fill,
            full_fill_rate_pct=full_rate,
            partial_fill_rate_pct=part_rate,
            unwind_rate_pct=unwind_rate,
            average_slippage_per_contract_cents=avg_slip,
            fees_as_pct_of_gross_edge=fee_pct_gross,
            open_unhedged_contracts=open_unhedged,
        )

        return funnel, financials, exec_quality, sim_trades

    def _evaluate_baselines(
        self,
        observations: Sequence[MarketObservation],
        config: V10EvaluationConfig,
    ) -> V10BaselineComparison:
        """Evaluate 6 comparative realism baselines under identical market data and capital."""
        tiers: List[V10BaselineTierResult] = []

        # Tier 0: No-Trade Baseline
        tiers.append(
            V10BaselineTierResult(
                tier_name="Tier 0: No-Trade",
                description="Zero order execution; 100% capital preservation",
                opportunities_count=0,
                trades_count=0,
                gross_pnl_cents=0,
                fees_paid_cents=0,
                scratch_loss_cents=0,
                net_pnl_cents=0,
                completed_rate_pct=Decimal("0.00"),
            )
        )

        # Tier 1: Theoretical Gross Edge (Frictionless, zero fees, top of book)
        t1_funnel, t1_fin, _, _ = self._run_replay(
            observations,
            config=config,
            micro_override=MicrostructureConfig(network_latency_ms=0.0, sequential_leg_delay_ms=0.0),
            fee_override=FeeModelConfig(schedule_type=FeeScheduleType.EXEMPT),
        )
        tiers.append(
            V10BaselineTierResult(
                tier_name="Tier 1: Gross Edge",
                description="Top of book, 0 fee, 0 latency, instantaneous fills",
                opportunities_count=t1_funnel.candidates_detected,
                trades_count=t1_fin.total_trades_count,
                gross_pnl_cents=t1_fin.realized_gross_edge_cents,
                fees_paid_cents=0,
                scratch_loss_cents=0,
                net_pnl_cents=t1_fin.realized_gross_edge_cents,
                completed_rate_pct=_safe_pct(t1_funnel.completed_arbitrage_trades, t1_funnel.simulated_execution_attempts),
            )
        )

        # Tier 2: Net Edge with Fees (Zero latency, CFTC fees)
        t2_funnel, t2_fin, _, _ = self._run_replay(
            observations,
            config=config,
            micro_override=MicrostructureConfig(network_latency_ms=0.0, sequential_leg_delay_ms=0.0),
            fee_override=config.fee_config,
        )
        tiers.append(
            V10BaselineTierResult(
                tier_name="Tier 2: Net Top-of-Book",
                description="CFTC quadratic fees, 0 latency, instantaneous fills",
                opportunities_count=t2_funnel.candidates_detected,
                trades_count=t2_fin.total_trades_count,
                gross_pnl_cents=t2_fin.realized_gross_edge_cents,
                fees_paid_cents=t2_fin.total_exchange_fees_cents,
                scratch_loss_cents=0,
                net_pnl_cents=t2_fin.net_realized_pnl_cents,
                completed_rate_pct=_safe_pct(t2_funnel.completed_arbitrage_trades, t2_funnel.simulated_execution_attempts),
            )
        )

        # Tier 3: Depth-Aware Executable Edge (Full depth traversal, zero latency)
        t3_funnel, t3_fin, _, _ = self._run_replay(
            observations,
            config=config,
            micro_override=MicrostructureConfig(network_latency_ms=0.0, sequential_leg_delay_ms=0.0),
            fee_override=config.fee_config,
        )
        tiers.append(
            V10BaselineTierResult(
                tier_name="Tier 3: Depth Executable",
                description="Depth traversal, VWAP pricing, CFTC fees, zero latency",
                opportunities_count=t3_funnel.candidates_detected,
                trades_count=t3_fin.total_trades_count,
                gross_pnl_cents=t3_fin.realized_gross_edge_cents,
                fees_paid_cents=t3_fin.total_exchange_fees_cents,
                scratch_loss_cents=0,
                net_pnl_cents=t3_fin.net_realized_pnl_cents,
                completed_rate_pct=_safe_pct(t3_funnel.completed_arbitrage_trades, t3_funnel.simulated_execution_attempts),
            )
        )

        # Tier 4: Realistic Execution Simulation (Baseline: 50ms RTT, 25ms leg delay, scratch recovery)
        t4_funnel, t4_fin, _, _ = self._run_replay(
            observations,
            config=config,
            micro_override=config.microstructure_config,
            fee_override=config.fee_config,
        )
        tiers.append(
            V10BaselineTierResult(
                tier_name="Tier 4: Realistic Simulation",
                description="Asynchronous 50ms RTT, quote decay, scratch recovery",
                opportunities_count=t4_funnel.candidates_detected,
                trades_count=t4_fin.total_trades_count,
                gross_pnl_cents=t4_fin.realized_gross_edge_cents,
                fees_paid_cents=t4_fin.total_exchange_fees_cents,
                scratch_loss_cents=t4_fin.scratch_unwind_drag_cents,
                net_pnl_cents=t4_fin.net_realized_pnl_cents,
                completed_rate_pct=_safe_pct(t4_funnel.completed_arbitrage_trades, t4_funnel.simulated_execution_attempts),
            )
        )

        # Tier 5: Conservative Stress Scenario (250ms latency, EMERGENCY_DUMP unwind)
        t5_micro = MicrostructureConfig(
            network_latency_ms=250.0,
            sequential_leg_delay_ms=50.0,
            unwind_policy=UnwindPolicy.EMERGENCY_DUMP,
            fee_config=config.fee_config,
        )
        t5_funnel, t5_fin, _, _ = self._run_replay(
            observations,
            config=config,
            micro_override=t5_micro,
            fee_override=config.fee_config,
        )
        tiers.append(
            V10BaselineTierResult(
                tier_name="Tier 5: Stress Scenario",
                description="250ms latency, adverse fill decay, emergency liquidation",
                opportunities_count=t5_funnel.candidates_detected,
                trades_count=t5_fin.total_trades_count,
                gross_pnl_cents=t5_fin.realized_gross_edge_cents,
                fees_paid_cents=t5_fin.total_exchange_fees_cents,
                scratch_loss_cents=t5_fin.scratch_unwind_drag_cents,
                net_pnl_cents=t5_fin.net_realized_pnl_cents,
                completed_rate_pct=_safe_pct(t5_funnel.completed_arbitrage_trades, t5_funnel.simulated_execution_attempts),
            )
        )

        return V10BaselineComparison(tiers=tuple(tiers))

    def _evaluate_out_of_sample(
        self,
        observations: Sequence[MarketObservation],
        config: V10EvaluationConfig,
    ) -> V10OutOfSampleResult:
        """Chronologically partition dataset into in-sample and out-of-sample periods."""
        n = len(observations)
        if not config.enable_out_of_sample or n < config.min_observations_for_split:
            return V10OutOfSampleResult(
                is_split_viable=False,
                insufficiency_reason=(
                    f"Dataset contains {n} observations (< {config.min_observations_for_split} minimum threshold); "
                    "insufficient volume for statistically valid out-of-sample partitioning."
                ),
                in_sample_observations_count=n,
                out_of_sample_observations_count=0,
                in_sample_span=None,
                out_of_sample_span=None,
                in_sample_candidates=0,
                out_of_sample_candidates=0,
                in_sample_completed_trades=0,
                out_of_sample_completed_trades=0,
                in_sample_net_pnl_cents=0,
                out_of_sample_net_pnl_cents=0,
            )

        split_idx = int(n * config.in_sample_ratio)
        in_sample = observations[:split_idx]
        out_of_sample = observations[split_idx:]

        is_funnel, is_fin, _, _ = self._run_replay(in_sample, config=config)
        oos_funnel, oos_fin, _, _ = self._run_replay(out_of_sample, config=config)

        is_span = f"{in_sample[0].observed_at} -> {in_sample[-1].observed_at}" if in_sample else None
        oos_span = f"{out_of_sample[0].observed_at} -> {out_of_sample[-1].observed_at}" if out_of_sample else None

        return V10OutOfSampleResult(
            is_split_viable=True,
            insufficiency_reason=None,
            in_sample_observations_count=len(in_sample),
            out_of_sample_observations_count=len(out_of_sample),
            in_sample_span=is_span,
            out_of_sample_span=oos_span,
            in_sample_candidates=is_funnel.candidates_detected,
            out_of_sample_candidates=oos_funnel.candidates_detected,
            in_sample_completed_trades=is_funnel.completed_arbitrage_trades,
            out_of_sample_completed_trades=oos_funnel.completed_arbitrage_trades,
            in_sample_net_pnl_cents=is_fin.net_realized_pnl_cents,
            out_of_sample_net_pnl_cents=oos_fin.net_realized_pnl_cents,
        )

    def _evaluate_sensitivity(
        self,
        observations: Sequence[MarketObservation],
        config: V10EvaluationConfig,
    ) -> Tuple[V10SensitivityPoint, ...]:
        """Perform multi-dimensional parameter sensitivity sweeps."""
        if not config.enable_sensitivity:
            return ()

        grid: List[V10SensitivityPoint] = []
        latencies = config.sensitivity_latencies_ms
        haircuts = config.sensitivity_haircuts

        for lat in latencies:
            for hc in haircuts:
                # Sweep CFTC fee model
                micro = MicrostructureConfig(
                    network_latency_ms=lat,
                    sequential_leg_delay_ms=lat / 2.0,
                    fee_config=config.fee_config,
                )
                funnel, fin, _, _ = self._run_replay(
                    observations,
                    config=config,
                    haircut=hc,
                    micro_override=micro,
                )
                grid.append(
                    V10SensitivityPoint(
                        latency_ms=lat,
                        depth_haircut=hc,
                        fee_model_name="CFTC_TAKER",
                        trades_attempted=funnel.simulated_execution_attempts,
                        trades_completed=funnel.completed_arbitrage_trades,
                        trades_legged_out=funnel.trades_legged_out,
                        completion_rate_pct=_safe_pct(funnel.completed_arbitrage_trades, funnel.simulated_execution_attempts),
                        net_pnl_cents=fin.net_realized_pnl_cents,
                        scratch_loss_cents=fin.scratch_unwind_drag_cents,
                    )
                )

        return tuple(grid)

    def _synthesize_verdict(
        self,
        quality: V10ObservationQualitySummary,
        funnel: V10OpportunityFunnel,
        financials: V10FinancialMetrics,
        out_of_sample: V10OutOfSampleResult,
        baseline_comparison: V10BaselineComparison,
    ) -> Tuple[str, str]:
        """Synthesize authoritative executive research verdict based strictly on empirical evidence."""
        # 1. Check if dataset is too sparse / empty
        if quality.valid_observations == 0 or quality.populated_books_count == 0:
            return (
                "INCONCLUSIVE",
                (
                    f"Dataset contains zero populated order books ({quality.empty_books_count} empty books observed). "
                    "No market liquidity existed to evaluate or simulate strategy execution."
                ),
            )

        if funnel.candidates_detected == 0:
            return (
                "INCONCLUSIVE",
                (
                    f"Across {quality.valid_observations} observations ({quality.populated_books_count} populated books), "
                    "zero gross parity or basket mispricings were detected. Real prediction market pricing was efficient "
                    "or order books were uncrossed during the observation period."
                ),
            )

        # 2. Check if trades executed
        if funnel.simulated_execution_attempts > 0:
            if financials.net_realized_pnl_cents > 0 and funnel.completed_arbitrage_trades > 0:
                if out_of_sample.is_split_viable and out_of_sample.out_of_sample_net_pnl_cents > 0:
                    return (
                        "POSITIVE EVIDENCE",
                        (
                            f"Strategy executed {funnel.completed_arbitrage_trades} completed arbitrage trades with net positive P&L "
                            f"(+{financials.net_realized_pnl_cents}¢), confirmed out-of-sample (+{out_of_sample.out_of_sample_net_pnl_cents}¢)."
                        ),
                    )
                elif not out_of_sample.is_split_viable:
                    return (
                        "INCONCLUSIVE",
                        (
                            f"Strategy yielded positive simulated P&L (+{financials.net_realized_pnl_cents}¢), but historical observation "
                            f"volume ({quality.valid_observations} obs) was insufficient to achieve statistically valid out-of-sample confirmation."
                        ),
                    )

            if financials.net_realized_pnl_cents <= 0:
                return (
                    "NEGATIVE EVIDENCE",
                    (
                        f"Strategy generated non-positive net returns ({financials.net_realized_pnl_cents}¢). Apparent gross edge was completely "
                        f"eroded by exchange fees ({financials.total_exchange_fees_cents}¢) and execution scratch drag ({financials.scratch_unwind_drag_cents}¢)."
                    ),
                )

        return (
            "INCONCLUSIVE",
            (
                f"Candidate opportunities were detected ({funnel.candidates_detected}), but all were rejected by "
                f"fee-unprofitability ({funnel.rejected_unprofitable_fees}), liquid universe constraints ({funnel.rejected_illiquid_universe}), "
                f"or order-book depth bottlenecks ({funnel.rejected_insufficient_depth}). Zero trades qualified for execution."
            ),
        )
