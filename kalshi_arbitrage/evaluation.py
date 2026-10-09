"""V6 — Historical Opportunity Evaluation & Paper-Trading Performance.

Provides a deterministic research and evaluation workflow that ingests historical
market observations, applies V2 arbitrage detection and V3 execution pricing,
tracks opportunity lifecycles and executable depth, simulates paper trading with
exact ledger accounting and reconciliation, and performs parameter sensitivity analysis.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

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
from .contract import NO, YES
from .evidence_storage import (
    CorruptedEvidenceError,
    EvidenceStorageError,
    EvidenceStore,
    observation_from_dict,
    opportunity_from_dict,
)
from .execution import (
    ExecutionPricingResult,
    STATUS_INSUFFICIENT_DEPTH,
    STATUS_INVALID_INPUT,
    STATUS_NO_GROSS_EDGE,
    evaluate_binary_parity_execution,
    evaluate_mece_basket_execution,
)
from .ledger import PaperPortfolio, ReconciliationResult
from .market_data import NormalizedMarket, NormalizedOrderBook
from .observer import (
    DeclaredMeceBasket,
    EvaluatedOpportunity,
    MarketObservation,
    STATUS_QUALIFIED,
    STATUS_REJECTED_INSUFFICIENT_DEPTH,
    STATUS_REJECTED_INVALID_INPUT,
    STATUS_REJECTED_LEG_SKEW,
    STATUS_REJECTED_MISSING_MARKET,
    STATUS_REJECTED_MISSING_TIMESTAMP,
    STATUS_REJECTED_NO_GROSS_EDGE,
    STATUS_REJECTED_RISK,
    STATUS_REJECTED_STALE,
    STATUS_REJECTED_UNPROFITABLE_FEES,
    generate_observation_id,
    make_binary_parity_opportunity_id,
    make_mece_opportunity_id,
)
from .paper_executor import PaperExecutionEngine, PaperExecutionError
from .paper_trade import PaperTrade, TradeState
from .portfolio import Event
from .risk import RiskConfig, RiskManager


class EvaluationError(Exception):
    """Base exception for evaluation configuration and runtime errors."""


_INHERIT: Any = object()


def _normalize_timestamp(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize datetime to UTC for timezone-safe comparisons."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Evaluation Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvaluationConfig:
    """Explicit parameters governing historical opportunity evaluation.

    Paper trading is disabled by default to prevent accidental state mutation
    during read-only research and analysis.
    """

    initial_cash_cents: int = 100_000
    target_quantity: int = 1
    default_fee_per_contract_cents: Optional[int] = 1
    max_stale_seconds: float = 60.0
    max_future_seconds: float = 10.0
    max_leg_timestamp_skew_seconds: float = 5.0
    require_source_timestamp: bool = True
    enforce_net_profitability: bool = True
    enable_paper_trading: bool = False  # Disabled by default
    conservative_haircut_cents: int = 0  # Modeled slippage buffer per contract
    episode_timeout_seconds: float = 120.0  # Max inactivity before starting new opportunity episode
    risk_config: Optional[RiskConfig] = None
    declared_baskets: Tuple[DeclaredMeceBasket, ...] = ()
    deduplicate_observations: bool = True
    strict_data_validation: bool = False

    def __post_init__(self) -> None:
        if self.initial_cash_cents <= 0:
            raise EvaluationError("initial_cash_cents must be positive")
        if self.target_quantity <= 0:
            raise EvaluationError("target_quantity must be positive")
        if (
            self.default_fee_per_contract_cents is not None
            and self.default_fee_per_contract_cents < 0
        ):
            raise EvaluationError("default_fee_per_contract_cents must be non-negative")
        if self.max_stale_seconds <= 0:
            raise EvaluationError("max_stale_seconds must be positive")
        if self.max_future_seconds < 0:
            raise EvaluationError("max_future_seconds must be non-negative")
        if self.max_leg_timestamp_skew_seconds <= 0:
            raise EvaluationError("max_leg_timestamp_skew_seconds must be positive")
        if self.conservative_haircut_cents < 0:
            raise EvaluationError("conservative_haircut_cents must be non-negative")
        if self.episode_timeout_seconds <= 0:
            raise EvaluationError("episode_timeout_seconds must be positive")


# ---------------------------------------------------------------------------
# Historical Dataset
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HistoricalDataset:
    """Immutable collection of historical observations with explicit provenance and quality metrics."""

    observations: Tuple[MarketObservation, ...]
    source_description: str
    total_raw_records: int
    malformed_records_count: int
    duplicate_records_count: int
    out_of_order_records_count: int
    earliest_timestamp: Optional[datetime]
    latest_timestamp: Optional[datetime]
    unique_tickers: Tuple[str, ...]

    @classmethod
    def from_observations(
        cls,
        observations: Sequence[MarketObservation],
        source_description: str = "in-memory",
        deduplicate: bool = True,
    ) -> "HistoricalDataset":
        """Construct dataset from in-memory MarketObservation objects with chronological validation."""
        raw_count = len(observations)
        if raw_count == 0:
            return cls(
                observations=(),
                source_description=source_description,
                total_raw_records=0,
                malformed_records_count=0,
                duplicate_records_count=0,
                out_of_order_records_count=0,
                earliest_timestamp=None,
                latest_timestamp=None,
                unique_tickers=(),
            )

        seen_keys: Set[Tuple[str, str]] = set()
        cleaned_obs: List[MarketObservation] = []
        dup_count = 0
        out_of_order_count = 0

        max_ts: Optional[datetime] = None
        for obs in observations:
            key = (obs.ticker, obs.observed_at.isoformat())
            if key in seen_keys:
                dup_count += 1
                if deduplicate:
                    continue
            seen_keys.add(key)

            obs_ts_norm = _normalize_timestamp(obs.observed_at)
            if max_ts is not None and obs_ts_norm < max_ts:
                out_of_order_count += 1
            else:
                max_ts = obs_ts_norm
            cleaned_obs.append(obs)

        # Sort chronologically to eliminate look-ahead bias and ensure determinism
        sorted_obs = tuple(sorted(cleaned_obs, key=lambda o: _normalize_timestamp(o.observed_at)))
        earliest = sorted_obs[0].observed_at if sorted_obs else None
        latest = sorted_obs[-1].observed_at if sorted_obs else None
        tickers = tuple(sorted({o.ticker for o in sorted_obs}))

        return cls(
            observations=sorted_obs,
            source_description=source_description,
            total_raw_records=raw_count,
            malformed_records_count=0,
            duplicate_records_count=dup_count,
            out_of_order_records_count=out_of_order_count,
            earliest_timestamp=earliest,
            latest_timestamp=latest,
            unique_tickers=tickers,
        )

    @classmethod
    def from_file(
        cls,
        path: Union[str, Path],
        *,
        deduplicate: bool = True,
        strict: bool = False,
    ) -> "HistoricalDataset":
        """Load and validate dataset from a JSONL file."""
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Historical evidence file not found: {file_path}")

        raw_count = 0
        malformed_count = 0
        raw_obs: List[MarketObservation] = []

        with open(file_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                clean = line.strip()
                if not clean:
                    continue
                raw_count += 1
                try:
                    payload = json.loads(clean)
                    obs = observation_from_dict(payload)
                    raw_obs.append(obs)
                except Exception as exc:
                    malformed_count += 1
                    if strict:
                        raise CorruptedEvidenceError(
                            f"Malformed record at {file_path}:{line_no}: {exc}"
                        ) from exc

        dataset = cls.from_observations(
            raw_obs,
            source_description=str(file_path),
            deduplicate=deduplicate,
        )
        return cls(
            observations=dataset.observations,
            source_description=str(file_path),
            total_raw_records=raw_count,
            malformed_records_count=malformed_count,
            duplicate_records_count=dataset.duplicate_records_count,
            out_of_order_records_count=dataset.out_of_order_records_count,
            earliest_timestamp=dataset.earliest_timestamp,
            latest_timestamp=dataset.latest_timestamp,
            unique_tickers=dataset.unique_tickers,
        )

    @classmethod
    def from_store(
        cls,
        store: EvidenceStore,
        *,
        deduplicate: bool = True,
        strict: bool = False,
    ) -> "HistoricalDataset":
        """Load dataset from an EvidenceStore."""
        return cls.from_file(
            store.observations_path,
            deduplicate=deduplicate,
            strict=strict,
        )


# ---------------------------------------------------------------------------
# Opportunity Evaluation Record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OpportunityEvaluationRecord:
    """Detailed record of an evaluated arbitrage opportunity with complete provenance."""

    evaluation_id: str
    opportunity_id: str
    strategy_type: str
    observed_at: datetime
    market_tickers: Tuple[str, ...]
    is_fresh: bool
    staleness_reason: Optional[str]
    source_timestamps: Mapping[str, Optional[datetime]]
    leg_timestamp_skew_seconds: float
    theoretical_gross_edge_cents: int
    supported_quantity: int
    entry_cost_cents: int
    expected_payout_cents: int
    configured_fee_cents: Optional[int]
    conservative_haircut_cents: int
    net_edge_cents: Optional[int]
    net_return_pct: Optional[Decimal]
    is_qualified: bool
    status: str
    rejection_reason: Optional[str]
    is_recurrent: bool
    paper_trade_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Evaluation Metrics & Report
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvaluationMetrics:
    """Structured statistical metrics across historical observations, candidates, and execution."""

    total_observations: int
    valid_observations: int
    stale_observations: int
    failed_observations: int
    candidates_detected: int
    qualified_opportunities: int
    rejected_opportunities: int
    rejections_by_reason: Dict[str, int]
    rejections_by_status: Dict[str, int]
    supported_quantity_min: int
    supported_quantity_max: int
    supported_quantity_mean: Decimal
    gross_edge_min_cents: int
    gross_edge_max_cents: int
    gross_edge_mean_cents: Decimal
    net_edge_min_cents: Optional[int]
    net_edge_max_cents: Optional[int]
    net_edge_mean_cents: Optional[Decimal]
    paper_trades_submitted: int
    paper_trades_filled: int
    paper_trades_partially_filled: int
    paper_trades_rejected: int
    paper_fill_rate: Optional[Decimal]
    paper_total_fees_cents: int
    paper_realized_pnl_cents: int
    paper_unrealized_pnl_cents: int
    paper_final_cash_cents: Optional[int]
    paper_final_equity_cents: Optional[int]
    paper_max_drawdown_cents: int
    paper_max_drawdown_pct: Decimal
    ledger_is_reconciled: bool
    strategy_counts: Dict[str, int]


@dataclass(frozen=True)
class EvaluationReport:
    """Comprehensive historical evaluation report with full audit trail."""

    config: EvaluationConfig
    dataset_summary: Dict[str, Any]
    metrics: EvaluationMetrics
    evaluated_records: Tuple[OpportunityEvaluationRecord, ...]
    paper_trades: Tuple[PaperTrade, ...]
    paper_portfolio: Optional[PaperPortfolio] = None

    def summary(self) -> str:
        """Produce an auditable, clear text report suitable for research review."""
        m = self.metrics
        ds = self.dataset_summary
        cfg = self.config

        lines = [
            "=" * 72,
            "KALSHI ARBITRAGE ENGINE — V6 HISTORICAL OPPORTUNITY EVALUATION",
            "=" * 72,
            "EVALUATION CONFIGURATION (ASSUMPTIONS):",
            f"  Default Fee per Contract:          {cfg.default_fee_per_contract_cents if cfg.default_fee_per_contract_cents is not None else 'None'}¢",
            f"  Conservative Slippage Haircut:     {cfg.conservative_haircut_cents}¢",
            f"  Max Stale Tolerance:               {cfg.max_stale_seconds:.1f}s",
            f"  Max Future Drift Tolerance:        {cfg.max_future_seconds:.1f}s",
            f"  Max Leg Timestamp Skew:            {cfg.max_leg_timestamp_skew_seconds:.1f}s",
            f"  Enforce Net Profitability:         {cfg.enforce_net_profitability}",
            f"  Paper Trading Simulation:          {'ENABLED' if cfg.enable_paper_trading else 'DISABLED (Evaluation Only)'}",
            "",
            "HISTORICAL DATASET PROVENANCE:",
            f"  Source Description:                {ds.get('source_description', 'N/A')}",
            f"  Total Raw Records:                 {ds.get('total_raw_records', 0)}",
            f"  Valid Observations Loaded:         {ds.get('valid_observations_count', 0)}",
            f"  Malformed / Corrupted Records:     {ds.get('malformed_records_count', 0)}",
            f"  Duplicate Records Deduplicated:    {ds.get('duplicate_records_count', 0)}",
            f"  Out-of-Order Records Ordered:      {ds.get('out_of_order_records_count', 0)}",
            f"  Time Span:                         {ds.get('earliest_timestamp', 'None')} -> {ds.get('latest_timestamp', 'None')}",
            f"  Unique Tickers Observed:           {ds.get('unique_tickers_count', 0)}",
            "",
            "DATA QUALITY & FRESHNESS:",
            f"  Processed Observations:            {m.total_observations}",
            f"  Fresh / Valid Order Books:         {m.valid_observations}",
            f"  Stale / Drifted Order Books:       {m.stale_observations}",
            f"  Failed / Missing Order Books:      {m.failed_observations}",
            "",
            "OPPORTUNITY DETECTION & QUALIFICATION:",
            f"  Theoretical Candidates Detected:   {m.candidates_detected}",
            f"  Qualified (Depth & Positive Net):  {m.qualified_opportunities}",
            f"  Rejected Candidates:               {m.rejected_opportunities}",
            "",
            "REJECTION BREAKDOWN:",
        ]

        if m.rejections_by_reason:
            for r, c in sorted(m.rejections_by_reason.items(), key=lambda kv: -kv[1]):
                lines.append(f"  - {r}: {c}")
        else:
            lines.append("  (None)")

        lines.extend([
            "",
            "LIQUIDITY & PROFITABILITY DISTRIBUTIONS:",
            f"  Executable Quantity (Min/Mean/Max): {m.supported_quantity_min} / {m.supported_quantity_mean:.2f} / {m.supported_quantity_max}",
            f"  Gross Edge (Min/Mean/Max):          {m.gross_edge_min_cents}¢ / {m.gross_edge_mean_cents:.2f}¢ / {m.gross_edge_max_cents}¢",
        ])

        if m.net_edge_mean_cents is not None:
            lines.append(
                f"  Net Edge (Min/Mean/Max):            {m.net_edge_min_cents}¢ / {m.net_edge_mean_cents:.2f}¢ / {m.net_edge_max_cents}¢"
            )
        else:
            lines.append("  Net Edge:                           (Unconfigured fees)")

        if m.strategy_counts:
            lines.extend(["", "CANDIDATES BY STRATEGY:"])
            for strat, cnt in sorted(m.strategy_counts.items()):
                lines.append(f"  - {strat}: {cnt}")

        if cfg.enable_paper_trading:
            lines.extend([
                "",
                "V4 PAPER TRADING PERFORMANCE:",
                f"  Simulated Trades Submitted:        {m.paper_trades_submitted}",
                f"  Simulated Trades Fully Filled:     {m.paper_trades_filled}",
                f"  Simulated Trades Partial:          {m.paper_trades_partially_filled}",
                f"  Simulated Trades Rejected:         {m.paper_trades_rejected}",
                f"  Fill Rate (Filled / Submitted):    {f'{m.paper_fill_rate:.1%}' if m.paper_fill_rate is not None else 'N/A'}",
                f"  Total Simulated Fees Paid:         {m.paper_total_fees_cents}¢ (${cents_to_dollars(m.paper_total_fees_cents):.2f})",
                f"  Realized P&L (Settled):            {m.paper_realized_pnl_cents}¢ (${cents_to_dollars(m.paper_realized_pnl_cents):.2f})",
                f"  Unrealized P&L (Book Value):       {m.paper_unrealized_pnl_cents}¢ (${cents_to_dollars(m.paper_unrealized_pnl_cents):.2f})",
                f"  Final Available Cash:              {m.paper_final_cash_cents}¢ (${cents_to_dollars(m.paper_final_cash_cents or 0):.2f})",
                f"  Final Portfolio Book Equity:       {m.paper_final_equity_cents}¢ (${cents_to_dollars(m.paper_final_equity_cents or 0):.2f})",
                f"  Max Drawdown (Book Equity):        {m.paper_max_drawdown_cents}¢ ({m.paper_max_drawdown_pct:.2f}%)",
                f"  Ledger Internal Consistency:       {'RECONCILED' if m.ledger_is_reconciled else 'DISCREPANCY DETECTED'}",
            ])

        lines.extend([
            "",
            "=" * 72,
            "RESEARCH DEFINITIONS & DISCLAIMERS:",
            "1. Theoretical Detection vs Executable Opportunity:",
            "   Top-of-book prices imply potential edge, but real viability requires observable",
            "   counter-party depth, volume-weighted pricing, and exchange transaction fees.",
            "2. Data Freshness Policy:",
            "   Order books are evaluated using exchange HTTP Date response headers. Stale or",
            "   future-drifted snapshots are excluded to prevent simulated phantom fills.",
            "3. Paper Trading Accounting Policy:",
            "   Book-value equity equals cash plus position acquisition cost basis. Unrealized",
            "   arbitrage edge is realized only upon final market settlement. Drawdown during",
            "   simulation reflects transaction fees paid prior to contract resolution.",
            "4. Simulation Limitations:",
            "   Simulated paper execution does not model queue priority, matching engine latency,",
            "   cancellations, or adverse selection. Snapshot depth does not prove continuous liquidity.",
            "=" * 72,
        ])
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Convert report to serializable dictionary for JSON exports."""
        m = self.metrics
        ds = self.dataset_summary
        cfg = self.config
        return {
            "config": {
                "initial_cash_cents": cfg.initial_cash_cents,
                "target_quantity": cfg.target_quantity,
                "default_fee_per_contract_cents": cfg.default_fee_per_contract_cents,
                "conservative_haircut_cents": cfg.conservative_haircut_cents,
                "max_stale_seconds": cfg.max_stale_seconds,
                "max_future_seconds": cfg.max_future_seconds,
                "max_leg_timestamp_skew_seconds": cfg.max_leg_timestamp_skew_seconds,
                "enforce_net_profitability": cfg.enforce_net_profitability,
                "enable_paper_trading": cfg.enable_paper_trading,
            },
            "dataset_summary": ds,
            "metrics": {
                "total_observations": m.total_observations,
                "valid_observations": m.valid_observations,
                "stale_observations": m.stale_observations,
                "failed_observations": m.failed_observations,
                "candidates_detected": m.candidates_detected,
                "qualified_opportunities": m.qualified_opportunities,
                "rejected_opportunities": m.rejected_opportunities,
                "rejections_by_reason": m.rejections_by_reason,
                "supported_quantity_min": m.supported_quantity_min,
                "supported_quantity_max": m.supported_quantity_max,
                "supported_quantity_mean": str(m.supported_quantity_mean),
                "gross_edge_min_cents": m.gross_edge_min_cents,
                "gross_edge_max_cents": m.gross_edge_max_cents,
                "gross_edge_mean_cents": str(m.gross_edge_mean_cents),
                "net_edge_min_cents": m.net_edge_min_cents,
                "net_edge_max_cents": m.net_edge_max_cents,
                "net_edge_mean_cents": str(m.net_edge_mean_cents) if m.net_edge_mean_cents else None,
                "paper_trades_submitted": m.paper_trades_submitted,
                "paper_trades_filled": m.paper_trades_filled,
                "paper_trades_partially_filled": m.paper_trades_partially_filled,
                "paper_trades_rejected": m.paper_trades_rejected,
                "paper_fill_rate": str(m.paper_fill_rate) if m.paper_fill_rate else None,
                "paper_total_fees_cents": m.paper_total_fees_cents,
                "paper_realized_pnl_cents": m.paper_realized_pnl_cents,
                "paper_unrealized_pnl_cents": m.paper_unrealized_pnl_cents,
                "paper_final_cash_cents": m.paper_final_cash_cents,
                "paper_final_equity_cents": m.paper_final_equity_cents,
                "paper_max_drawdown_cents": m.paper_max_drawdown_cents,
                "paper_max_drawdown_pct": str(m.paper_max_drawdown_pct),
                "ledger_is_reconciled": m.ledger_is_reconciled,
                "strategy_counts": m.strategy_counts,
            },
        }


# ---------------------------------------------------------------------------
# Sensitivity Analysis
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SensitivityProfile:
    """A specific parameter combination to evaluate in sensitivity analysis."""

    name: str
    fee_per_contract_cents: Any = _INHERIT
    max_stale_seconds: Any = _INHERIT
    conservative_haircut_cents: Any = _INHERIT
    target_quantity: Any = _INHERIT


@dataclass(frozen=True)
class SensitivityRow:
    """Summary metrics for one evaluation profile in sensitivity analysis."""

    name: str
    fee_per_contract_cents: Optional[int]
    conservative_haircut_cents: int
    max_stale_seconds: float
    target_quantity: int
    qualified_count: int
    qualification_rate_pct: Decimal
    rejections_unprofitable_fees: int
    rejections_insufficient_depth: int
    rejections_stale: int
    mean_executable_quantity: Decimal
    mean_net_edge_cents: Optional[Decimal]
    simulated_net_profit_cents: Optional[int] = None
    simulated_fees_cents: Optional[int] = None


@dataclass(frozen=True)
class SensitivityReport:
    """Comparative analysis showing how opportunity viability varies across assumptions."""

    base_config: EvaluationConfig
    dataset_summary: Dict[str, Any]
    rows: Tuple[SensitivityRow, ...]

    def summary(self) -> str:
        """Render a formatted comparison table."""
        lines = [
            "=" * 92,
            "KALSHI ARBITRAGE ENGINE — SENSITIVITY ANALYSIS REPORT",
            "=" * 92,
            f"{'Profile Name':<16} | {'Fee':<4} | {'Haircut':<7} | {'Stale':<5} | {'Qty':<4} | {'Qualified':<9} | {'Rate':<6} | {'Mean Net':<9} | {'Sim P&L':<8}",
            "-" * 92,
        ]

        for r in self.rows:
            fee_str = f"{r.fee_per_contract_cents}¢" if r.fee_per_contract_cents is not None else "None"
            haircut_str = f"{r.conservative_haircut_cents}¢"
            stale_str = f"{int(r.max_stale_seconds)}s"
            net_str = f"{r.mean_net_edge_cents:.2f}¢" if r.mean_net_edge_cents is not None else "N/A"
            pnl_str = f"{r.simulated_net_profit_cents}¢" if r.simulated_net_profit_cents is not None else "N/A"

            lines.append(
                f"{r.name:<16} | {fee_str:<4} | {haircut_str:<7} | {stale_str:<5} | {r.target_quantity:<4} | {r.qualified_count:<9} | {r.qualification_rate_pct:>5.1f}% | {net_str:<9} | {pnl_str:<8}"
            )

        lines.extend([
            "=" * 92,
            "KEY SENSITIVITY INSIGHTS:",
            "- Impact of Transaction Fees: Shows whether theoretical edges withstand exchange fees.",
            "- Impact of Freshness Thresholds: Highlights risk of relying on stale book snapshots.",
            "- Impact of Conservative Haircut: Tests fragility of thin margins against adverse fills.",
            "=" * 92,
        ])
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Historical Evaluator Implementation
# ---------------------------------------------------------------------------

class HistoricalEvaluator:
    """Deterministic historical opportunity evaluation engine."""

    def __init__(self, config: Optional[EvaluationConfig] = None):
        self.config = config or EvaluationConfig()

    def evaluate(self, dataset: HistoricalDataset) -> EvaluationReport:
        """Run complete chronological evaluation across historical observations."""
        records: List[OpportunityEvaluationRecord] = []
        paper_trades: List[PaperTrade] = []

        portfolio: Optional[PaperPortfolio] = None
        risk_manager: Optional[RiskManager] = None
        executor: Optional[PaperExecutionEngine] = None

        if self.config.enable_paper_trading:
            portfolio = PaperPortfolio(initial_cash_cents=self.config.initial_cash_cents)
            risk_manager = RiskManager(self.config.risk_config or RiskConfig())
            executor = PaperExecutionEngine(
                default_fee_per_contract_cents=self.config.default_fee_per_contract_cents
            )

        # Active opportunity episode tracking for recurrence & deduplication
        active_episodes: Dict[str, datetime] = {}
        processed_trade_ids: Set[str] = set()

        peak_equity_cents = self.config.initial_cash_cents
        max_drawdown_cents = 0

        # Index observations by ticker for fast basket leg lookups
        ticker_latest_obs: Dict[str, MarketObservation] = {}

        for obs in dataset.observations:
            ticker = obs.ticker
            now = obs.observed_at

            # Update latest observation for ticker
            ticker_latest_obs[ticker] = obs

            # 1. Single-market Binary Parity Evaluation
            opp_record = self._evaluate_observation_binary_parity(
                obs,
                active_episodes,
            )

            if opp_record is not None:
                # 2. Paper Trading Simulation (if enabled and qualified)
                if self.config.enable_paper_trading and executor and portfolio and risk_manager:
                    if opp_record.is_qualified and not opp_record.is_recurrent:
                        paper_trade = self._simulate_paper_trade(
                            obs,
                            opp_record,
                            executor,
                            portfolio,
                            risk_manager,
                        )
                        paper_trades.append(paper_trade)
                        opp_record = dataclass_replace_paper_trade_id(
                            opp_record, paper_trade.trade_id
                        )

                        # Update drawdown
                        curr_eq = portfolio.portfolio_equity_cents()
                        if curr_eq > peak_equity_cents:
                            peak_equity_cents = curr_eq
                        dd = peak_equity_cents - curr_eq
                        if dd > max_drawdown_cents:
                            max_drawdown_cents = dd

                records.append(opp_record)

            # 3. Declared MECE Basket Evaluation
            for basket in self.config.declared_baskets:
                # Check if this observation is one of the legs
                if ticker in basket.market_outcome_map:
                    basket_record = self._evaluate_mece_basket(
                        basket,
                        ticker_latest_obs,
                        now,
                        active_episodes,
                    )
                    if basket_record is not None:
                        if self.config.enable_paper_trading and executor and portfolio and risk_manager:
                            if basket_record.is_qualified and not basket_record.is_recurrent:
                                paper_trade = self._simulate_paper_trade_mece(
                                    basket,
                                    ticker_latest_obs,
                                    basket_record,
                                    executor,
                                    portfolio,
                                    risk_manager,
                                )
                                paper_trades.append(paper_trade)
                                basket_record = dataclass_replace_paper_trade_id(
                                    basket_record, paper_trade.trade_id
                                )

                                # Update drawdown
                                curr_eq = portfolio.portfolio_equity_cents()
                                if curr_eq > peak_equity_cents:
                                    peak_equity_cents = curr_eq
                                dd = peak_equity_cents - curr_eq
                                if dd > max_drawdown_cents:
                                    max_drawdown_cents = dd

                        records.append(basket_record)

        # Final ledger reconciliation
        ledger_reconciled = True
        final_cash: Optional[int] = None
        final_equity: Optional[int] = None
        realized_pnl = 0
        unrealized_pnl = 0
        total_fees = 0

        if portfolio is not None:
            rec = portfolio.reconcile()
            ledger_reconciled = rec.is_reconciled
            final_cash = portfolio.available_cash_cents
            final_equity = portfolio.portfolio_equity_cents()
            realized_pnl = portfolio.realized_pnl_cents
            unrealized_pnl = 0  # Valued at cost basis
            total_fees = portfolio.total_fees_paid_cents

        # Compute summary metrics
        metrics = self._compute_metrics(
            dataset,
            records,
            paper_trades,
            ledger_reconciled,
            final_cash,
            final_equity,
            realized_pnl,
            unrealized_pnl,
            total_fees,
            max_drawdown_cents,
            peak_equity_cents,
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
            "unique_tickers": dataset.unique_tickers,
        }

        return EvaluationReport(
            config=self.config,
            dataset_summary=dataset_summary,
            metrics=metrics,
            evaluated_records=tuple(records),
            paper_trades=tuple(paper_trades),
            paper_portfolio=portfolio,
        )

    def _evaluate_observation_binary_parity(
        self,
        obs: MarketObservation,
        active_episodes: Dict[str, datetime],
    ) -> Optional[OpportunityEvaluationRecord]:
        """Evaluate a single market observation for binary parity arbitrage."""
        if not obs.is_success or obs.market is None or obs.order_book is None:
            return None

        opp_id = make_binary_parity_opportunity_id(obs.ticker)

        # V2 Candidate Detection
        try:
            candidate = evaluate_market_parity(
                obs.market,
                quantity=self.config.target_quantity,
                fee_per_contract_cents=self.config.default_fee_per_contract_cents,
            )
        except Exception:
            return None

        # Check freshness & future drift BEFORE assessing disappearance or qualification
        source_ts = obs.source_timestamp
        source_ts_map = {obs.ticker: source_ts}
        is_fresh = True
        staleness_reason: Optional[str] = None

        if obs.is_stale:
            is_fresh = False
            staleness_reason = obs.staleness_reason or "Market observation marked as stale"
        elif source_ts is None:
            if self.config.require_source_timestamp:
                is_fresh = False
                staleness_reason = "Missing exchange source timestamp"
        else:
            norm_obs = _normalize_timestamp(obs.observed_at)
            norm_src = _normalize_timestamp(source_ts)
            age_seconds = (norm_obs - norm_src).total_seconds()
            if age_seconds < -self.config.max_future_seconds:
                is_fresh = False
                staleness_reason = f"Source timestamp is from the future ({age_seconds:.1f}s)"
            elif age_seconds > self.config.max_stale_seconds:
                is_fresh = False
                staleness_reason = (
                    f"Order book is stale (age: {age_seconds:.1f}s > max: {self.config.max_stale_seconds:.1f}s)"
                )

        # Disappearance condition: ONLY a fresh, trustworthy observation can signal that an active episode ended.
        if not candidate.is_arbitrage and candidate.gross_edge_cents <= 0:
            if is_fresh and opp_id in active_episodes:
                del active_episodes[opp_id]
            return None

        eval_id = f"eval-{generate_observation_id()}"

        # Check recurrence & episode tracking
        last_seen = active_episodes.get(opp_id)
        is_recurrent = False
        if last_seen is not None:
            norm_obs = _normalize_timestamp(obs.observed_at)
            norm_last = _normalize_timestamp(last_seen)
            gap_seconds = (norm_obs - norm_last).total_seconds()
            if gap_seconds <= self.config.episode_timeout_seconds:
                is_recurrent = True
            else:
                is_recurrent = False

        # If stale, reject fail-closed without pricing execution.
        # Do not activate episode if it was not already active (avoids blocking later fresh trades).
        if not is_fresh:
            if is_recurrent:
                active_episodes[opp_id] = obs.observed_at
            status = (
                STATUS_REJECTED_MISSING_TIMESTAMP
                if staleness_reason == "Missing exchange source timestamp"
                else STATUS_REJECTED_STALE
            )
            return OpportunityEvaluationRecord(
                evaluation_id=eval_id,
                opportunity_id=opp_id,
                strategy_type=OPPORTUNITY_BINARY_PARITY,
                observed_at=obs.observed_at,
                market_tickers=(obs.ticker,),
                is_fresh=False,
                staleness_reason=staleness_reason,
                source_timestamps=source_ts_map,
                leg_timestamp_skew_seconds=0.0,
                theoretical_gross_edge_cents=candidate.gross_edge_cents,
                supported_quantity=0,
                entry_cost_cents=0,
                expected_payout_cents=0,
                configured_fee_cents=self.config.default_fee_per_contract_cents,
                conservative_haircut_cents=self.config.conservative_haircut_cents,
                net_edge_cents=None,
                net_return_pct=None,
                is_qualified=False,
                status=status,
                rejection_reason=staleness_reason,
                is_recurrent=is_recurrent,
            )

        # Fresh observation updates active episode
        active_episodes[opp_id] = obs.observed_at

        # V3 Execution Pricing (passes exact configured fee, preserving None)
        pricing = evaluate_binary_parity_execution(
            obs.order_book,
            requested_quantity=self.config.target_quantity,
            fee_per_contract_cents=self.config.default_fee_per_contract_cents,
        )

        is_qualified = False
        status = pricing.status
        rejection_reason = pricing.rejection_reason

        # Haircut calculation kept separate from exchange fees
        total_contracts = sum(t.supported_quantity for t in pricing.leg_traversals)
        total_haircut_cents = total_contracts * self.config.conservative_haircut_cents
        modeled_net_profit_cents: Optional[int] = None
        modeled_is_net_profitable: Optional[bool] = None

        if pricing.net_profit_cents is not None:
            modeled_net_profit_cents = pricing.net_profit_cents - total_haircut_cents
            modeled_is_net_profitable = modeled_net_profit_cents > 0
        elif self.config.default_fee_per_contract_cents is None:
            modeled_is_net_profitable = None

        if pricing.supported_quantity == 0:
            status = STATUS_REJECTED_INSUFFICIENT_DEPTH
            rejection_reason = "Order book has no available ask liquidity"
        elif not pricing.is_gross_profitable:
            status = STATUS_REJECTED_NO_GROSS_EDGE
            rejection_reason = "No gross edge when traversing order book depth"
        elif self.config.enforce_net_profitability and modeled_is_net_profitable is False:
            status = STATUS_REJECTED_UNPROFITABLE_FEES
            rejection_reason = "Unprofitable after estimated fees and haircut"
        elif self.config.enforce_net_profitability and modeled_is_net_profitable is None:
            status = STATUS_REJECTED_UNPROFITABLE_FEES
            rejection_reason = "Net profitability cannot be verified without fee configuration"
        else:
            status = STATUS_QUALIFIED
            is_qualified = True
            rejection_reason = None

        net_return_pct = None
        if modeled_net_profit_cents is not None and pricing.total_cost_cents > 0:
            net_return_pct = (
                Decimal(modeled_net_profit_cents) / Decimal(pricing.total_cost_cents)
            ) * Decimal(100)

        return OpportunityEvaluationRecord(
            evaluation_id=eval_id,
            opportunity_id=opp_id,
            strategy_type=OPPORTUNITY_BINARY_PARITY,
            observed_at=obs.observed_at,
            market_tickers=(obs.ticker,),
            is_fresh=True,
            staleness_reason=None,
            source_timestamps=source_ts_map,
            leg_timestamp_skew_seconds=0.0,
            theoretical_gross_edge_cents=candidate.gross_edge_cents,
            supported_quantity=pricing.supported_quantity,
            entry_cost_cents=pricing.total_cost_cents,
            expected_payout_cents=pricing.guaranteed_payout_cents,
            configured_fee_cents=self.config.default_fee_per_contract_cents,
            conservative_haircut_cents=self.config.conservative_haircut_cents,
            net_edge_cents=modeled_net_profit_cents,
            net_return_pct=net_return_pct,
            is_qualified=is_qualified,
            status=status,
            rejection_reason=rejection_reason,
            is_recurrent=is_recurrent,
        )

    def _evaluate_mece_basket(
        self,
        basket: DeclaredMeceBasket,
        ticker_latest_obs: Dict[str, MarketObservation],
        observed_at: datetime,
        active_episodes: Dict[str, datetime],
    ) -> Optional[OpportunityEvaluationRecord]:
        """Evaluate a declared MECE event basket candidate from available observations."""
        strat_type = (
            OPPORTUNITY_MECE_BASKET_LONG_YES
            if basket.basket_side == YES
            else OPPORTUNITY_MECE_BASKET_LONG_NO
        )
        opp_id = make_mece_opportunity_id(basket.event_ticker, basket.basket_side)
        eval_id = f"eval-{generate_observation_id()}"

        # Check all required legs exist
        missing_tickers = [t for t in basket.market_outcome_map if t not in ticker_latest_obs]
        if missing_tickers:
            return None

        basket_obs = [ticker_latest_obs[t] for t in basket.market_outcome_map]
        if any(not o.is_success or o.market is None or o.order_book is None for o in basket_obs):
            return None

        markets = [o.market for o in basket_obs]
        books_by_outcome = {
            basket.market_outcome_map[o.ticker]: o.order_book for o in basket_obs
        }
        source_ts_map = {o.ticker: o.source_timestamp for o in basket_obs}

        event = Event(
            identifier=basket.event_ticker,
            outcomes=basket.outcomes,
            relationship_established=True,
        )

        try:
            candidate = evaluate_mece_markets(
                event=event,
                markets=markets,
                market_outcome_map=basket.market_outcome_map,
                basket_side=basket.basket_side,
                quantity=self.config.target_quantity,
                fee_per_contract_cents=self.config.default_fee_per_contract_cents,
            )
        except Exception:
            return None

        # Check freshness & future drift across EVERY leg
        is_fresh = True
        staleness_reason: Optional[str] = None
        rejection_status = STATUS_REJECTED_STALE
        norm_observed_at = _normalize_timestamp(observed_at)

        for o in basket_obs:
            if o.is_stale:
                is_fresh = False
                staleness_reason = o.staleness_reason or f"Leg {o.ticker} is marked as stale"
                rejection_status = STATUS_REJECTED_STALE
                break
            elif o.source_timestamp is None:
                if self.config.require_source_timestamp:
                    is_fresh = False
                    staleness_reason = f"Missing source timestamp for leg {o.ticker}"
                    rejection_status = STATUS_REJECTED_MISSING_TIMESTAMP
                    break
            else:
                norm_leg_ts = _normalize_timestamp(o.source_timestamp)
                age_seconds = (norm_observed_at - norm_leg_ts).total_seconds()
                if age_seconds < -self.config.max_future_seconds:
                    is_fresh = False
                    staleness_reason = f"Source timestamp for leg {o.ticker} is from the future ({age_seconds:.1f}s)"
                    rejection_status = STATUS_REJECTED_STALE
                    break
                elif age_seconds > self.config.max_stale_seconds:
                    is_fresh = False
                    staleness_reason = (
                        f"Order book for leg {o.ticker} is stale "
                        f"(age: {age_seconds:.1f}s > max: {self.config.max_stale_seconds:.1f}s)"
                    )
                    rejection_status = STATUS_REJECTED_STALE
                    break

        # Check multi-leg skew with normalized timestamps
        times: List[float] = [
            _normalize_timestamp(o.source_timestamp).timestamp()
            for o in basket_obs
            if o.source_timestamp is not None
        ]
        skew_seconds = max(times) - min(times) if len(times) > 1 else 0.0
        if is_fresh and skew_seconds > self.config.max_leg_timestamp_skew_seconds:
            is_fresh = False
            staleness_reason = (
                f"Leg timestamp skew {skew_seconds:.2f}s > max {self.config.max_leg_timestamp_skew_seconds:.2f}s"
            )
            rejection_status = STATUS_REJECTED_LEG_SKEW

        # Check recurrence & episode tracking
        last_seen = active_episodes.get(opp_id)
        is_recurrent = False
        is_intermediate_update = False
        if last_seen is not None:
            norm_last = _normalize_timestamp(last_seen)
            gap_seconds = (norm_observed_at - norm_last).total_seconds()
            if gap_seconds <= self.config.episode_timeout_seconds:
                is_recurrent = True
            else:
                is_recurrent = False

            # Detect desynchronized intermediate state:
            # When an active episode exists, if some legs have received updates newer than last_seen
            # while other legs still reflect older snapshots (observed at or prior to last_seen),
            # this is an intermediate/interleaved tick.
            has_newer_leg = any(_normalize_timestamp(o.observed_at) > norm_last for o in basket_obs)
            has_older_leg = any(_normalize_timestamp(o.observed_at) <= norm_last for o in basket_obs)
            is_intermediate_update = has_newer_leg and has_older_leg

        # Disappearance condition:
        # ONLY a fresh, trustworthy, fully synchronized snapshot (not an intermediate leg update)
        # can signal that an active MECE opportunity has disappeared.
        if not candidate.is_arbitrage and candidate.gross_edge_cents <= 0:
            if is_fresh and not is_intermediate_update and opp_id in active_episodes:
                del active_episodes[opp_id]
            return None

        if not is_fresh:
            if is_recurrent:
                active_episodes[opp_id] = observed_at
            return OpportunityEvaluationRecord(
                evaluation_id=eval_id,
                opportunity_id=opp_id,
                strategy_type=strat_type,
                observed_at=observed_at,
                market_tickers=basket.ordered_tickers,
                is_fresh=False,
                staleness_reason=staleness_reason,
                source_timestamps=source_ts_map,
                leg_timestamp_skew_seconds=skew_seconds,
                theoretical_gross_edge_cents=candidate.gross_edge_cents,
                supported_quantity=0,
                entry_cost_cents=0,
                expected_payout_cents=0,
                configured_fee_cents=self.config.default_fee_per_contract_cents,
                conservative_haircut_cents=self.config.conservative_haircut_cents,
                net_edge_cents=None,
                net_return_pct=None,
                is_qualified=False,
                status=rejection_status,
                rejection_reason=staleness_reason,
                is_recurrent=is_recurrent,
            )

        active_episodes[opp_id] = observed_at

        # V3 Execution Pricing (preserving None)
        pricing = evaluate_mece_basket_execution(
            event=event,
            books_by_outcome=books_by_outcome,
            basket_side=basket.basket_side,
            requested_quantity=self.config.target_quantity,
            fee_per_contract_cents=self.config.default_fee_per_contract_cents,
        )

        is_qualified = False
        status = pricing.status
        rejection_reason = pricing.rejection_reason

        # Haircut calculation kept separate from exchange fees
        total_contracts = sum(t.supported_quantity for t in pricing.leg_traversals)
        total_haircut_cents = total_contracts * self.config.conservative_haircut_cents
        modeled_net_profit_cents: Optional[int] = None
        modeled_is_net_profitable: Optional[bool] = None

        if pricing.net_profit_cents is not None:
            modeled_net_profit_cents = pricing.net_profit_cents - total_haircut_cents
            modeled_is_net_profitable = modeled_net_profit_cents > 0
        elif self.config.default_fee_per_contract_cents is None:
            modeled_is_net_profitable = None

        if pricing.supported_quantity == 0:
            status = STATUS_REJECTED_INSUFFICIENT_DEPTH
            rejection_reason = "Order books have insufficient depth for common basket execution"
        elif not pricing.is_gross_profitable:
            status = STATUS_REJECTED_NO_GROSS_EDGE
            rejection_reason = "No gross edge across combined basket execution"
        elif self.config.enforce_net_profitability and modeled_is_net_profitable is False:
            status = STATUS_REJECTED_UNPROFITABLE_FEES
            rejection_reason = "Unprofitable after estimated basket fees and haircut"
        elif self.config.enforce_net_profitability and modeled_is_net_profitable is None:
            status = STATUS_REJECTED_UNPROFITABLE_FEES
            rejection_reason = "Net profitability cannot be verified without fee configuration"
        else:
            status = STATUS_QUALIFIED
            is_qualified = True
            rejection_reason = None

        net_return_pct = None
        if modeled_net_profit_cents is not None and pricing.total_cost_cents > 0:
            net_return_pct = (
                Decimal(modeled_net_profit_cents) / Decimal(pricing.total_cost_cents)
            ) * Decimal(100)

        return OpportunityEvaluationRecord(
            evaluation_id=eval_id,
            opportunity_id=opp_id,
            strategy_type=strat_type,
            observed_at=observed_at,
            market_tickers=basket.ordered_tickers,
            is_fresh=True,
            staleness_reason=None,
            source_timestamps=source_ts_map,
            leg_timestamp_skew_seconds=skew_seconds,
            theoretical_gross_edge_cents=candidate.gross_edge_cents,
            supported_quantity=pricing.supported_quantity,
            entry_cost_cents=pricing.total_cost_cents,
            expected_payout_cents=pricing.guaranteed_payout_cents,
            configured_fee_cents=self.config.default_fee_per_contract_cents,
            conservative_haircut_cents=self.config.conservative_haircut_cents,
            net_edge_cents=modeled_net_profit_cents,
            net_return_pct=net_return_pct,
            is_qualified=is_qualified,
            status=status,
            rejection_reason=rejection_reason,
            is_recurrent=is_recurrent,
        )

    def _simulate_paper_trade(
        self,
        obs: MarketObservation,
        record: OpportunityEvaluationRecord,
        executor: PaperExecutionEngine,
        portfolio: PaperPortfolio,
        risk_manager: RiskManager,
    ) -> PaperTrade:
        """Simulate execution fill and apply to ledger with strict pre-trade risk checks."""
        timestamp_str = obs.observed_at.isoformat()

        trade = executor.simulate_binary_parity(
            obs.order_book,
            requested_quantity=self.config.target_quantity,
            fee_per_contract_cents=self.config.default_fee_per_contract_cents,
            timestamp=timestamp_str,
            portfolio=portfolio,
            risk_manager=risk_manager,
        )

        if trade.state in (TradeState.FILLED, TradeState.PARTIALLY_FILLED):
            portfolio.apply_trade_fill(trade, timestamp=timestamp_str)
            risk_manager.record_processed_trade(trade.trade_id)

        return trade

    def _simulate_paper_trade_mece(
        self,
        basket: DeclaredMeceBasket,
        ticker_latest_obs: Dict[str, MarketObservation],
        record: OpportunityEvaluationRecord,
        executor: PaperExecutionEngine,
        portfolio: PaperPortfolio,
        risk_manager: RiskManager,
    ) -> PaperTrade:
        """Simulate execution fill for a qualified MECE basket and apply to ledger."""
        timestamp_str = record.observed_at.isoformat()
        event = Event(
            identifier=basket.event_ticker,
            outcomes=basket.outcomes,
            relationship_established=True,
        )
        basket_obs = [ticker_latest_obs[t] for t in basket.market_outcome_map]
        books_by_outcome = {
            basket.market_outcome_map[o.ticker]: o.order_book for o in basket_obs
        }

        trade = executor.simulate_mece_basket(
            event=event,
            books_by_outcome=books_by_outcome,
            basket_side=basket.basket_side,
            requested_quantity=self.config.target_quantity,
            fee_per_contract_cents=self.config.default_fee_per_contract_cents,
            timestamp=timestamp_str,
            portfolio=portfolio,
            risk_manager=risk_manager,
        )

        if trade.state in (TradeState.FILLED, TradeState.PARTIALLY_FILLED):
            portfolio.apply_trade_fill(trade, timestamp=timestamp_str)
            risk_manager.record_processed_trade(trade.trade_id)

        return trade

    def _compute_metrics(
        self,
        dataset: HistoricalDataset,
        records: Sequence[OpportunityEvaluationRecord],
        paper_trades: Sequence[PaperTrade],
        ledger_reconciled: bool,
        final_cash: Optional[int],
        final_equity: Optional[int],
        realized_pnl: int,
        unrealized_pnl: int,
        total_fees: int,
        max_drawdown_cents: int,
        peak_equity_cents: int,
    ) -> EvaluationMetrics:
        """Compute structured summary metrics across all evaluated records."""
        total_obs = len(dataset.observations)
        valid_obs = sum(1 for o in dataset.observations if o.is_success and not o.is_stale)
        stale_obs = sum(1 for o in dataset.observations if o.is_stale)
        failed_obs = sum(1 for o in dataset.observations if not o.is_success)

        candidates = len(records)
        qualified = sum(1 for r in records if r.is_qualified)
        rejected = sum(1 for r in records if not r.is_qualified)

        rejections: Dict[str, int] = {}
        rejections_by_status: Dict[str, int] = {}
        quantities: List[int] = []
        gross_edges: List[int] = []
        net_edges: List[int] = []
        strat_counts: Dict[str, int] = {}

        for r in records:
            strat_counts[r.strategy_type] = strat_counts.get(r.strategy_type, 0) + 1
            if not r.is_qualified:
                reason = r.rejection_reason or r.status
                rejections[reason] = rejections.get(reason, 0) + 1
                rejections_by_status[r.status] = rejections_by_status.get(r.status, 0) + 1

            if r.supported_quantity > 0:
                quantities.append(r.supported_quantity)
            gross_edges.append(r.theoretical_gross_edge_cents)
            if r.net_edge_cents is not None:
                net_edges.append(r.net_edge_cents)

        qty_min = min(quantities) if quantities else 0
        qty_max = max(quantities) if quantities else 0
        qty_mean = (
            Decimal(sum(quantities)) / Decimal(len(quantities)) if quantities else Decimal("0")
        )

        gross_min = min(gross_edges) if gross_edges else 0
        gross_max = max(gross_edges) if gross_edges else 0
        gross_mean = (
            Decimal(sum(gross_edges)) / Decimal(len(gross_edges)) if gross_edges else Decimal("0")
        )

        net_min = min(net_edges) if net_edges else None
        net_max = max(net_edges) if net_edges else None
        net_mean = (
            Decimal(sum(net_edges)) / Decimal(len(net_edges)) if net_edges else None
        )

        submitted_trades = len(paper_trades)
        filled_trades = sum(1 for t in paper_trades if t.state == TradeState.FILLED)
        partial_trades = sum(1 for t in paper_trades if t.state == TradeState.PARTIALLY_FILLED)
        rejected_trades = sum(1 for t in paper_trades if t.state == TradeState.REJECTED)

        fill_rate = (
            Decimal(filled_trades + partial_trades) / Decimal(submitted_trades)
            if submitted_trades > 0
            else None
        )

        max_dd_pct = (
            (Decimal(max_drawdown_cents) / Decimal(peak_equity_cents)) * Decimal(100)
            if peak_equity_cents > 0
            else Decimal("0")
        )

        return EvaluationMetrics(
            total_observations=total_obs,
            valid_observations=valid_obs,
            stale_observations=stale_obs,
            failed_observations=failed_obs,
            candidates_detected=candidates,
            qualified_opportunities=qualified,
            rejected_opportunities=rejected,
            rejections_by_reason=rejections,
            rejections_by_status=rejections_by_status,
            supported_quantity_min=qty_min,
            supported_quantity_max=qty_max,
            supported_quantity_mean=qty_mean,
            gross_edge_min_cents=gross_min,
            gross_edge_max_cents=gross_max,
            gross_edge_mean_cents=gross_mean,
            net_edge_min_cents=net_min,
            net_edge_max_cents=net_max,
            net_edge_mean_cents=net_mean,
            paper_trades_submitted=submitted_trades,
            paper_trades_filled=filled_trades,
            paper_trades_partially_filled=partial_trades,
            paper_trades_rejected=rejected_trades,
            paper_fill_rate=fill_rate,
            paper_total_fees_cents=total_fees,
            paper_realized_pnl_cents=realized_pnl,
            paper_unrealized_pnl_cents=unrealized_pnl,
            paper_final_cash_cents=final_cash,
            paper_final_equity_cents=final_equity,
            paper_max_drawdown_cents=max_drawdown_cents,
            paper_max_drawdown_pct=max_dd_pct,
            ledger_is_reconciled=ledger_reconciled,
            strategy_counts=strat_counts,
        )


def dataclass_replace_paper_trade_id(
    record: OpportunityEvaluationRecord, paper_trade_id: str
) -> OpportunityEvaluationRecord:
    """Helper to associate paper trade ID with an evaluation record."""
    return OpportunityEvaluationRecord(
        evaluation_id=record.evaluation_id,
        opportunity_id=record.opportunity_id,
        strategy_type=record.strategy_type,
        observed_at=record.observed_at,
        market_tickers=record.market_tickers,
        is_fresh=record.is_fresh,
        staleness_reason=record.staleness_reason,
        source_timestamps=record.source_timestamps,
        leg_timestamp_skew_seconds=record.leg_timestamp_skew_seconds,
        theoretical_gross_edge_cents=record.theoretical_gross_edge_cents,
        supported_quantity=record.supported_quantity,
        entry_cost_cents=record.entry_cost_cents,
        expected_payout_cents=record.expected_payout_cents,
        configured_fee_cents=record.configured_fee_cents,
        conservative_haircut_cents=record.conservative_haircut_cents,
        net_edge_cents=record.net_edge_cents,
        net_return_pct=record.net_return_pct,
        is_qualified=record.is_qualified,
        status=record.status,
        rejection_reason=record.rejection_reason,
        is_recurrent=record.is_recurrent,
        paper_trade_id=paper_trade_id,
    )


# ---------------------------------------------------------------------------
# High-Level Evaluation & Sensitivity APIs
# ---------------------------------------------------------------------------

def evaluate_historical_evidence(
    source: Union[HistoricalDataset, EvidenceStore, str, Path],
    config: Optional[EvaluationConfig] = None,
) -> EvaluationReport:
    """Evaluate historical opportunities against explicit execution assumptions."""
    cfg = config or EvaluationConfig()
    if isinstance(source, HistoricalDataset):
        dataset = source
    elif isinstance(source, EvidenceStore):
        dataset = HistoricalDataset.from_store(
            source,
            deduplicate=cfg.deduplicate_observations,
            strict=cfg.strict_data_validation,
        )
    else:
        path = Path(source)
        if path.is_dir():
            # If directory, look for observations.jsonl
            obs_file = path / "observations.jsonl"
            if not obs_file.exists():
                raise FileNotFoundError(f"observations.jsonl not found in directory: {path}")
            dataset = HistoricalDataset.from_file(
                obs_file,
                deduplicate=cfg.deduplicate_observations,
                strict=cfg.strict_data_validation,
            )
        else:
            dataset = HistoricalDataset.from_file(
                path,
                deduplicate=cfg.deduplicate_observations,
                strict=cfg.strict_data_validation,
            )

    evaluator = HistoricalEvaluator(config=cfg)
    return evaluator.evaluate(dataset)


def run_sensitivity_analysis(
    dataset: HistoricalDataset,
    base_config: Optional[EvaluationConfig] = None,
    profiles: Optional[Sequence[SensitivityProfile]] = None,
) -> SensitivityReport:
    """Run sensitivity analysis across multiple execution assumption profiles."""
    cfg = base_config or EvaluationConfig()

    # Default profile suite exploring fees, freshness, and haircuts
    default_profiles = [
        SensitivityProfile("Zero Fees (0¢)", fee_per_contract_cents=0),
        SensitivityProfile("Standard Fees (1¢)", fee_per_contract_cents=1),
        SensitivityProfile("High Fees (2¢)", fee_per_contract_cents=2),
        SensitivityProfile("Severe Fees (3¢)", fee_per_contract_cents=3),
        SensitivityProfile("Tight Freshness (15s)", max_stale_seconds=15.0),
        SensitivityProfile("Standard Freshness (60s)", max_stale_seconds=60.0),
        SensitivityProfile("Relaxed Freshness (300s)", max_stale_seconds=300.0),
        SensitivityProfile("Slippage Haircut (1¢)", conservative_haircut_cents=1),
        SensitivityProfile("Slippage Haircut (2¢)", conservative_haircut_cents=2),
    ]

    selected_profiles = list(profiles or default_profiles)
    rows: List[SensitivityRow] = []

    for prof in selected_profiles:
        resolved_fee = (
            cfg.default_fee_per_contract_cents
            if prof.fee_per_contract_cents is _INHERIT
            else prof.fee_per_contract_cents
        )
        resolved_stale = (
            cfg.max_stale_seconds
            if prof.max_stale_seconds is _INHERIT
            else prof.max_stale_seconds
        )
        resolved_haircut = (
            cfg.conservative_haircut_cents
            if prof.conservative_haircut_cents is _INHERIT
            else prof.conservative_haircut_cents
        )
        resolved_qty = (
            cfg.target_quantity
            if prof.target_quantity is _INHERIT
            else prof.target_quantity
        )

        prof_cfg = EvaluationConfig(
            initial_cash_cents=cfg.initial_cash_cents,
            target_quantity=resolved_qty,
            default_fee_per_contract_cents=resolved_fee,
            max_stale_seconds=resolved_stale,
            max_future_seconds=cfg.max_future_seconds,
            max_leg_timestamp_skew_seconds=cfg.max_leg_timestamp_skew_seconds,
            require_source_timestamp=cfg.require_source_timestamp,
            enforce_net_profitability=cfg.enforce_net_profitability,
            enable_paper_trading=cfg.enable_paper_trading,
            conservative_haircut_cents=resolved_haircut,
            episode_timeout_seconds=cfg.episode_timeout_seconds,
            risk_config=cfg.risk_config,
            declared_baskets=cfg.declared_baskets,
            deduplicate_observations=cfg.deduplicate_observations,
            strict_data_validation=cfg.strict_data_validation,
        )

        evaluator = HistoricalEvaluator(prof_cfg)
        rep = evaluator.evaluate(dataset)
        m = rep.metrics

        total_cand = m.candidates_detected
        qual_rate = (
            (Decimal(m.qualified_opportunities) / Decimal(total_cand)) * Decimal(100)
            if total_cand > 0
            else Decimal("0")
        )

        net_profit: Optional[int] = None
        sim_fees: Optional[int] = None
        if cfg.enable_paper_trading and rep.paper_portfolio:
            net_profit = rep.paper_portfolio.portfolio_equity_cents() - cfg.initial_cash_cents
            sim_fees = rep.paper_portfolio.total_fees_paid_cents

        rows.append(
            SensitivityRow(
                name=prof.name,
                fee_per_contract_cents=resolved_fee,
                conservative_haircut_cents=resolved_haircut,
                max_stale_seconds=resolved_stale,
                target_quantity=resolved_qty,
                qualified_count=m.qualified_opportunities,
                qualification_rate_pct=qual_rate,
                rejections_unprofitable_fees=m.rejections_by_status.get(STATUS_REJECTED_UNPROFITABLE_FEES, 0),
                rejections_insufficient_depth=m.rejections_by_status.get(STATUS_REJECTED_INSUFFICIENT_DEPTH, 0),
                rejections_stale=(
                    m.rejections_by_status.get(STATUS_REJECTED_STALE, 0)
                    + m.rejections_by_status.get(STATUS_REJECTED_MISSING_TIMESTAMP, 0)
                ),
                mean_executable_quantity=m.supported_quantity_mean,
                mean_net_edge_cents=m.net_edge_mean_cents,
                simulated_net_profit_cents=net_profit,
                simulated_fees_cents=sim_fees,
            )
        )

    ds_summary = {
        "source_description": dataset.source_description,
        "total_observations": len(dataset.observations),
        "unique_tickers": dataset.unique_tickers,
    }

    return SensitivityReport(
        base_config=cfg,
        dataset_summary=ds_summary,
        rows=tuple(rows),
    )
