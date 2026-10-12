"""Kalshi Arbitrage Engine V9 — Autonomous Paper Trading Coordinator.

Coordinates the end-to-end autonomous paper trading runtime:
1. Lifecycle state management (STOPPED, STARTING, RUNNING, PAUSED, STOPPING, FAILED).
2. Explicit verification of paper mode (prohibits live order placement).
3. Resilient startup, schema validation, and crash-recovery reconciliation.
4. Point-in-time market data ingestion and freshness validation.
5. Liquid universe filtering and deterministic opportunity evaluation.
6. Pre-trade risk approval and capital reservation.
7. V8 asynchronous multi-leg paper execution and recovery.
8. Atomic persistence and continuous ledger reconciliation.
9. Bounded or continuous cycle execution with graceful shutdown.
10. Auditable cycle summaries and comprehensive session reporting.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
import uuid

from .async_execution import (
    AsyncMultiLegTrade,
    MultiLegLifecycleState,
    UnwindPolicy,
)
from .ledger import PaperPortfolio, ReconciliationResult
from .market_data import NormalizedMarket, NormalizedOrderBook
from .market_data_provider import (
    FixtureMarketDataProvider,
    MarketDataProvider,
    MarketDataProviderError,
    RestMarketDataProvider,
)
from .opportunity_evaluator import (
    AutonomousOpportunityEvaluator,
    EvaluatedCandidate,
    OpportunityEvaluatorConfig,
)
from .paper_execution import PaperExecutionConfig, PaperExecutionManager
from .paper_persistence import (
    PAPER_SCHEMA_VERSION,
    PaperSessionState,
    PaperStateStore,
)
from .paper_risk import PaperRiskConfig, PaperRiskGate
from .universe import (
    LiquidUniverseSelector,
    MarketLiquidityProfile,
    UniverseFilterConfig,
    UniverseSelectionResult,
)

logger = logging.getLogger(__name__)


class CoordinatorState(str, Enum):
    """Explicit runtime lifecycle states for the autonomous coordinator."""

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


class CoordinatorError(RuntimeError):
    """Raised when runtime orchestration or invariant validation fails."""


@dataclass(frozen=True)
class CycleSummary:
    """Auditable summary of an individual paper-trading cycle."""

    cycle_index: int
    started_at: str
    completed_at: str
    duration_seconds: float
    markets_discovered: int
    markets_refreshed: int
    markets_stale_or_invalid: int
    universe_qualified: int
    candidates_detected: int
    candidates_approved_risk: int
    candidates_rejected_risk: int
    trades_executed: int
    trades_completed_arbitrage: int
    trades_unwound_or_partial: int
    total_cost_cents: int
    total_fees_cents: int
    realized_pnl_delta_cents: int
    portfolio_cash_cents: int
    portfolio_equity_cents: int
    open_positions_count: int
    is_reconciled: bool
    errors: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PaperCoordinatorConfig:
    """Runtime orchestration parameters and subsystem configurations."""

    execution_mode: str = "paper"
    state_filepath: Path = Path("data/paper_trading_state.json")
    poll_interval_seconds: float = 5.0
    discovery_refresh_interval_seconds: float = 60.0
    max_cycles: Optional[int] = None
    max_markets_per_cycle: int = 50
    initial_cash_cents: int = 1_000_000  # $10,000.00
    clean_session: bool = False
    universe_config: UniverseFilterConfig = field(default_factory=UniverseFilterConfig)
    evaluator_config: OpportunityEvaluatorConfig = field(default_factory=OpportunityEvaluatorConfig)
    risk_config: PaperRiskConfig = field(default_factory=PaperRiskConfig)
    execution_config: PaperExecutionConfig = field(default_factory=PaperExecutionConfig)

    def __post_init__(self) -> None:
        if self.execution_mode != "paper":
            raise CoordinatorError(
                f"Prohibited execution mode {self.execution_mode!r}. Autonomous coordinator "
                f"requires execution_mode='paper' to prevent live order placement."
            )
        if self.poll_interval_seconds < 0:
            raise CoordinatorError("poll_interval_seconds cannot be negative")
        if self.discovery_refresh_interval_seconds <= 0:
            raise CoordinatorError("discovery_refresh_interval_seconds must be positive")
        if self.initial_cash_cents <= 0:
            raise CoordinatorError("initial_cash_cents must be positive")


class PaperTradingCoordinator:
    """Autonomous runtime coordinator managing the end-to-end paper trading loop."""

    def __init__(
        self,
        config: Optional[PaperCoordinatorConfig] = None,
        data_provider: Optional[MarketDataProvider] = None,
    ) -> None:
        self.config = config or PaperCoordinatorConfig()
        if self.config.execution_mode != "paper":
            raise CoordinatorError("Coordinator strictly requires execution_mode='paper'")

        self.state_store = PaperStateStore(self.config.state_filepath)
        self.data_provider = data_provider or RestMarketDataProvider()
        self.universe_selector = LiquidUniverseSelector(self.config.universe_config)
        self.evaluator = AutonomousOpportunityEvaluator(self.config.evaluator_config)
        self.risk_gate = PaperRiskGate(self.config.risk_config)
        self.execution_manager = PaperExecutionManager(self.config.execution_config)

        self._runtime_state = CoordinatorState.STOPPED
        self._session_state: Optional[PaperSessionState] = None
        self._cycle_count: int = 0
        self._last_discovery_time: Optional[datetime] = None
        self._discovered_markets: List[NormalizedMarket] = []
        self._stop_requested: bool = False
        self._last_error: Optional[str] = None
        self._cycle_summaries: List[CycleSummary] = []

    @property
    def runtime_state(self) -> CoordinatorState:
        return self._runtime_state

    @property
    def session_state(self) -> Optional[PaperSessionState]:
        return self._session_state

    @property
    def cycle_summaries(self) -> Tuple[CycleSummary, ...]:
        return tuple(self._cycle_summaries)

    def startup(self) -> None:
        """Executes the strict 9-step startup and recovery sequence."""
        self._runtime_state = CoordinatorState.STARTING
        logger.info(f"Starting Paper Trading Coordinator (mode={self.config.execution_mode})...")

        try:
            # 1 & 2. Verify config and mode
            if self.config.execution_mode != "paper":
                raise CoordinatorError("Execution mode is not 'paper'")

            # 3 & 4. Load or initialize persistent state
            if self.config.state_filepath.exists() and not self.config.clean_session:
                logger.info(f"Existing state found at {self.config.state_filepath}; initiating crash recovery...")
                self._session_state = self.state_store.load_and_recover()
                self._cycle_count = self._session_state.last_completed_cycle
            else:
                session_id = f"paper-session-{uuid.uuid4().hex[:8]}"
                logger.info(f"Creating new paper trading session {session_id}...")
                portfolio = PaperPortfolio(initial_cash_cents=self.config.initial_cash_cents)
                self._session_state = PaperSessionState(
                    session_id=session_id,
                    execution_mode="paper",
                    initial_cash_cents=self.config.initial_cash_cents,
                    risk_config=self.config.risk_config,
                    portfolio=portfolio,
                )
                self.state_store.save(self._session_state)
                self._cycle_count = 0

            # 5 & 6. Verify ledger reconciliation
            rec = self._session_state.portfolio.reconcile()
            if not rec.is_reconciled:
                raise CoordinatorError(f"Startup accounting reconciliation failed: {rec.discrepancies}")

            # 7 & 8. Verify data provider readiness
            stats = self.data_provider.get_stats()
            logger.info(f"Market data provider initialized (total_requests={stats.total_requests})")

            # 9. Enter RUNNING state
            self._runtime_state = CoordinatorState.RUNNING
            self._session_state.status = "RUNNING"
            self.state_store.save(self._session_state)
            logger.info(
                f"Coordinator running successfully (available_cash={self._session_state.portfolio.available_cash_cents}¢, "
                f"open_positions={len(self._session_state.portfolio.positions)})"
            )

        except Exception as exc:
            self._runtime_state = CoordinatorState.FAILED
            self._last_error = str(exc)
            logger.exception(f"Coordinator startup failed closed: {exc}")
            raise

    def stop(self) -> None:
        """Requests graceful shutdown."""
        logger.info("Graceful shutdown requested for Paper Trading Coordinator...")
        self._stop_requested = True
        if self._runtime_state == CoordinatorState.RUNNING:
            self._runtime_state = CoordinatorState.STOPPING

    def run_single_cycle(self) -> CycleSummary:
        """Executes a single end-to-end paper trading cycle with full audit trail."""
        if self._runtime_state != CoordinatorState.RUNNING:
            raise CoordinatorError(f"Cannot execute cycle while in state {self._runtime_state.value}")
        if self._session_state is None:
            raise CoordinatorError("Session state is uninitialized")

        t0 = datetime.now(timezone.utc)
        self._cycle_count += 1
        cycle_idx = self._cycle_count
        logger.debug(f"Starting cycle {cycle_idx} at {t0.isoformat()}...")

        errors: List[str] = []
        refreshed_count = 0
        stale_invalid_count = 0
        candidates_detected = 0
        approved_risk_count = 0
        rejected_risk_count = 0
        trades_executed = 0
        trades_completed_arb = 0
        trades_unwound = 0
        cycle_cost = 0
        cycle_fees = 0
        cycle_pnl = 0

        # 1. Check if permitted to continue
        if self.risk_gate.is_kill_switch_active:
            logger.warning(f"Kill-switch is active ({self.risk_gate.kill_switch_reason}); skipping trade actions.")

        # 2. Market discovery refresh
        should_refresh_discovery = (
            self._last_discovery_time is None
            or (t0 - self._last_discovery_time).total_seconds() >= self.config.discovery_refresh_interval_seconds
        )
        if should_refresh_discovery:
            try:
                self._discovered_markets = list(
                    self.data_provider.discover_markets(limit=self.config.max_markets_per_cycle)
                )
                self._last_discovery_time = t0
                logger.info(f"Market discovery refreshed: found {len(self._discovered_markets)} markets.")
            except Exception as exc:
                err = f"Market discovery failed: {exc}"
                logger.error(err)
                errors.append(err)
                self.risk_gate.record_failure(err)

        discovered_count = len(self._discovered_markets)
        markets_to_evaluate = self._discovered_markets[: self.config.max_markets_per_cycle]

        # 3, 4 & 5. Fetch order books, validate freshness, and filter universe
        qualified_profiles: List[Tuple[NormalizedMarket, NormalizedOrderBook, MarketLiquidityProfile]] = []

        for market in markets_to_evaluate:
            try:
                book, obs_time = self.data_provider.get_order_book(market.ticker)
                refreshed_count += 1

                # Freshness validation
                val_res = self.data_provider.validate_market_and_book(
                    market, book, reference_time=t0, max_stale_seconds=self.config.risk_config.max_data_age_seconds
                )
                if not val_res.is_valid:
                    stale_invalid_count += 1
                    continue

                # Point-in-time universe selection
                profile = self.universe_selector.profile_market(market, book)
                if profile.is_selected:
                    qualified_profiles.append((market, book, profile))

            except Exception as exc:
                stale_invalid_count += 1
                logger.debug(f"Failed to refresh book for {market.ticker}: {exc}")

        # 6 & 7. Evaluate theoretical opportunities and execution pricing
        candidates: List[EvaluatedCandidate] = []
        for market, book, profile in qualified_profiles:
            cand = self.evaluator.evaluate_market_binary_parity(
                market=market,
                order_book=book,
                liquidity_profile=profile,
                current_cycle=cycle_idx,
                reference_time=t0,
            )
            if cand is not None:
                candidates_detected += 1
                candidates.append(cand)

        # 8. Deterministic ranking
        ranked_candidates = self.evaluator.rank_candidates(candidates)

        # 9 & 10. Risk approval and simulated execution
        books_by_ticker = {m.ticker: b for m, b, _ in qualified_profiles}

        for cand in ranked_candidates:
            if self.risk_gate.is_kill_switch_active:
                break

            risk_res = self.risk_gate.evaluate_candidate(
                self._session_state.portfolio, cand, current_time=t0
            )

            if not risk_res.passed:
                rejected_risk_count += 1
                logger.debug(f"Candidate {cand.candidate_id} rejected by risk: {risk_res.rejection_reason}")
                continue

            approved_risk_count += 1
            cand_book = books_by_ticker.get(cand.market_ticker)
            if cand_book is None:
                continue

            total_needed = cand.total_cost_cents + cand.total_fees_cents
            self.risk_gate.reserve_capital(self._session_state.portfolio, total_needed)
            try:
                # Simulated execution via V8 simulator
                trade = self.execution_manager.execute_candidate(
                    candidate=cand,
                    order_book=cand_book,
                    portfolio=self._session_state.portfolio,
                    simulated_time=t0,
                )

                # Record trade
                self._session_state.trades[trade.trade_id] = trade
                self.evaluator.mark_traded(cand.market_ticker, cycle_idx)
                self.risk_gate.record_success()

                trades_executed += 1
                if trade.is_arbitrage_complete:
                    trades_completed_arb += 1
                else:
                    trades_unwound += 1

                cycle_cost += trade.total_cost_cents
                cycle_fees += trade.total_fees_cents
                cycle_pnl += trade.realized_pnl_cents

            except Exception as exc:
                err = f"Execution failed for candidate {cand.candidate_id}: {exc}"
                logger.error(err)
                errors.append(err)
                self.risk_gate.record_failure(err)
            finally:
                self.risk_gate.release_reservation(self._session_state.portfolio, total_needed)

        # 11 & 12. Reconcile portfolio and persist state
        rec = self._session_state.portfolio.reconcile()
        if not rec.is_reconciled:
            err = f"Accounting invariant broken in cycle {cycle_idx}: {rec.discrepancies}"
            logger.critical(err)
            errors.append(err)
            self.risk_gate.activate_kill_switch(err)

        self._session_state.last_completed_cycle = cycle_idx
        t1 = datetime.now(timezone.utc)
        duration = (t1 - t0).total_seconds()

        # Build cycle summary
        summary = CycleSummary(
            cycle_index=cycle_idx,
            started_at=t0.isoformat(),
            completed_at=t1.isoformat(),
            duration_seconds=duration,
            markets_discovered=discovered_count,
            markets_refreshed=refreshed_count,
            markets_stale_or_invalid=stale_invalid_count,
            universe_qualified=len(qualified_profiles),
            candidates_detected=candidates_detected,
            candidates_approved_risk=approved_risk_count,
            candidates_rejected_risk=rejected_risk_count,
            trades_executed=trades_executed,
            trades_completed_arbitrage=trades_completed_arb,
            trades_unwound_or_partial=trades_unwound,
            total_cost_cents=cycle_cost,
            total_fees_cents=cycle_fees,
            realized_pnl_delta_cents=cycle_pnl,
            portfolio_cash_cents=self._session_state.portfolio.available_cash_cents,
            portfolio_equity_cents=self._session_state.portfolio.book_value_equity_cents(),
            open_positions_count=len(self._session_state.portfolio.positions),
            is_reconciled=rec.is_reconciled,
            errors=tuple(errors),
        )

        self._session_state.cycle_history.append(summary.to_dict())
        self._cycle_summaries.append(summary)

        # Save state atomically
        try:
            self.state_store.save(self._session_state)
        except Exception as exc:
            err = f"Persistence failure in cycle {cycle_idx}: {exc}"
            logger.error(err)
            errors.append(err)
            self.risk_gate.activate_kill_switch(err)

        return summary

    def run_loop(self) -> None:
        """Executes the coordinator polling loop until stopped or bounded max_cycles reached."""
        if self._runtime_state != CoordinatorState.RUNNING:
            self.startup()

        logger.info(
            f"Entering paper-trading loop (max_cycles={self.config.max_cycles}, "
            f"poll_interval={self.config.poll_interval_seconds}s)..."
        )

        try:
            while not self._stop_requested:
                if self.config.max_cycles is not None and self._cycle_count >= self.config.max_cycles:
                    logger.info(f"Reached configured maximum cycles ({self.config.max_cycles}); stopping loop.")
                    break

                self.run_single_cycle()

                if self._stop_requested:
                    break

                if self.config.poll_interval_seconds > 0:
                    time.sleep(self.config.poll_interval_seconds)

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received; halting coordinator loop.")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Performs clean shutdown and final state persistence."""
        self._runtime_state = CoordinatorState.STOPPING
        logger.info("Shutting down coordinator and persisting final state...")
        if self._session_state is not None:
            self._session_state.status = "STOPPED" if self._last_error is None else "FAILED"
            try:
                self.state_store.save(self._session_state)
            except Exception as exc:
                logger.error(f"Error persisting state during shutdown: {exc}")
        self._runtime_state = CoordinatorState.STOPPED
        logger.info("Paper Trading Coordinator stopped cleanly.")

    def generate_session_report(self) -> Dict[str, Any]:
        """Generates comprehensive auditable paper trading session report."""
        if self._session_state is None:
            return {"error": "Session state uninitialized"}

        portfolio = self._session_state.portfolio
        rec = portfolio.reconcile()
        trades = list(self._session_state.trades.values())

        full_arb = sum(1 for t in trades if t.is_arbitrage_complete)
        partial_or_unwound = len(trades) - full_arb
        total_fees = sum(t.total_fees_cents for t in trades)
        total_scratch = sum(t.scratch_loss_cents for t in trades)

        return {
            "session_id": self._session_state.session_id,
            "execution_mode": self._session_state.execution_mode,
            "created_at": self._session_state.created_at,
            "updated_at": self._session_state.updated_at,
            "total_cycles_completed": self._cycle_count,
            "initial_cash_cents": self._session_state.initial_cash_cents,
            "available_cash_cents": portfolio.available_cash_cents,
            "reserved_cash_cents": portfolio.reserved_cash_cents,
            "total_cost_basis_cents": portfolio.total_cost_basis_cents(),
            "book_value_equity_cents": portfolio.book_value_equity_cents(),
            "realized_pnl_cents": portfolio.realized_pnl_cents,
            "total_fees_cents": total_fees,
            "scratch_loss_cents": total_scratch,
            "total_trades_attempted": len(trades),
            "trades_completed_arbitrage": full_arb,
            "trades_partial_or_unwound": partial_or_unwound,
            "open_positions": [
                {
                    "contract_id": pos.contract_id,
                    "side": pos.side,
                    "quantity": pos.quantity,
                    "total_cost_cents": pos.total_cost_cents,
                }
                for pos in portfolio.positions.values()
            ],
            "ledger_entries_count": len(portfolio.ledger),
            "is_reconciled": rec.is_reconciled,
            "discrepancies": list(rec.discrepancies),
            "recovery_events_count": len(self._session_state.recovery_events),
            "kill_switch_active": self.risk_gate.is_kill_switch_active,
            "kill_switch_reason": self.risk_gate.kill_switch_reason,
        }
