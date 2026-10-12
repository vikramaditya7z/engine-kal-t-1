"""Lifecycle scenario validation suite for V9 Autonomous Paper Trading.

Formally tests Scenarios A through L specified in the Milestone V9 adversarial audit:
- Scenario A: No qualifying opportunity
- Scenario B: Valid opportunity and successful simulated execution
- Scenario C: Risk rejection
- Scenario D: Partial execution
- Scenario E: Unwind or scratch
- Scenario F: Execution failure or timeout
- Scenario G: Kill switch
- Scenario H: Stale or malformed market data
- Scenario I: Duplicate opportunities
- Scenario J: Restart and recovery
- Scenario K: Interrupted state recovery
- Scenario L: Clean shutdown and bounded termination
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import pytest

from kalshi_arbitrage.async_execution import (
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
from kalshi_arbitrage.contract import NO, YES
from kalshi_arbitrage.ledger import PaperPortfolio
from kalshi_arbitrage.coordinator import (
    CoordinatorError,
    CoordinatorState,
    CycleSummary,
    PaperCoordinatorConfig,
    PaperTradingCoordinator,
)
from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.market_data_provider import FixtureMarketDataProvider
from kalshi_arbitrage.paper_execution import PaperExecutionConfig, PaperExecutionManager
from kalshi_arbitrage.paper_persistence import (
    PaperSessionState,
    PaperStateStore,
)
from kalshi_arbitrage.paper_risk import PaperRiskConfig, PaperRiskGate
from kalshi_arbitrage.universe import UniverseFilterConfig


def _make_market_and_book(
    ticker: str,
    yes_bid: str,
    no_bid: str,
    qty: str = "10.00",
    volume: str = "100.00",
    oi: str = "200.00",
    timestamp: datetime = None,
):
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    market = NormalizedMarket(
        ticker=ticker,
        event_ticker=f"EVENT-{ticker}",
        market_type="binary",
        status="active",
        title=f"Market {ticker}",
        yes_subtitle="Yes",
        no_subtitle="No",
        volume_24h=Decimal(volume),
        open_interest=Decimal(oi),
        yes_bid_dollars=Decimal(yes_bid),
        no_bid_dollars=Decimal(no_bid),
    )
    book = NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(OrderBookLevel(price_dollars=Decimal(yes_bid), quantity=Decimal(qty)),),
        no_bids=(OrderBookLevel(price_dollars=Decimal(no_bid), quantity=Decimal(qty)),),
        source_timestamp=timestamp,
    )
    return market, book


# ==============================================================================
# Scenario A: No qualifying opportunity
# ==============================================================================
def test_scenario_a_no_qualifying_opportunity(tmp_path: Path):
    """Cycle with non-arbitrage market books executes 0 trades and preserves capital."""
    now = datetime.now(timezone.utc)
    # YES ask = 100 - 45 = 55¢, NO ask = 100 - 45 = 55¢. Total cost = 110¢ > 100¢ payout (no arb)
    m, b = _make_market_and_book("MKT-NO-ARB", yes_bid="0.45", no_bid="0.45", timestamp=now)
    data_prov = FixtureMarketDataProvider(markets=[m], books={m.ticker: b}, current_time=now)

    state_file = tmp_path / "scenario_a.json"
    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        initial_cash_cents=100_000,
        universe_config=UniverseFilterConfig(min_volume_24h=Decimal("1.00"), min_open_interest=Decimal("1.00")),
    )
    coord = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coord.startup()
    summary = coord.run_single_cycle()

    assert summary.candidates_detected == 1
    assert summary.candidates_approved_risk == 0
    assert summary.candidates_rejected_risk == 1
    assert summary.trades_executed == 0
    assert summary.portfolio_cash_cents == 100_000
    assert summary.open_positions_count == 0
    assert summary.is_reconciled is True
    coord.shutdown()


# ==============================================================================
# Scenario B: Valid opportunity and successful simulated execution
# ==============================================================================
def test_scenario_b_valid_opportunity_successful_execution(tmp_path: Path):
    """Valid binary parity opportunity passes risk and executes full arbitrage fill."""
    now = datetime.now(timezone.utc)
    # YES ask = 100 - 60 = 40¢, NO ask = 100 - 65 = 35¢. Total cost = 75¢. Payout = 100¢.
    m, b = _make_market_and_book("MKT-ARB-B", yes_bid="0.65", no_bid="0.60", qty="10", timestamp=now)
    data_prov = FixtureMarketDataProvider(markets=[m], books={m.ticker: b}, current_time=now)

    state_file = tmp_path / "scenario_b.json"
    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        initial_cash_cents=500_000,
        universe_config=UniverseFilterConfig(min_volume_24h=Decimal("1.00"), min_open_interest=Decimal("1.00")),
    )
    coord = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coord.startup()
    summary = coord.run_single_cycle()

    assert summary.candidates_detected == 1
    assert summary.candidates_approved_risk == 1
    assert summary.trades_executed == 1
    assert summary.trades_completed_arbitrage == 1
    assert summary.is_reconciled is True
    assert summary.portfolio_cash_cents < 500_000
    assert summary.open_positions_count == 2  # YES and NO positions
    coord.shutdown()


# ==============================================================================
# Scenario C: Risk rejection
# ==============================================================================
def test_scenario_c_risk_rejection(tmp_path: Path):
    """Opportunity violating max trade cost is rejected before execution."""
    now = datetime.now(timezone.utc)
    # Arb cost ~ 75¢. Set max_cost_per_trade to 50¢ so it is rejected.
    m, b = _make_market_and_book("MKT-ARB-C", yes_bid="0.65", no_bid="0.60", qty="10", timestamp=now)
    data_prov = FixtureMarketDataProvider(markets=[m], books={m.ticker: b}, current_time=now)

    state_file = tmp_path / "scenario_c.json"
    risk_cfg = PaperRiskConfig(max_cost_per_trade_cents=50)
    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        initial_cash_cents=100_000,
        risk_config=risk_cfg,
        universe_config=UniverseFilterConfig(min_volume_24h=Decimal("1.00"), min_open_interest=Decimal("1.00")),
    )
    coord = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coord.startup()
    summary = coord.run_single_cycle()

    assert summary.candidates_detected == 1
    assert summary.candidates_approved_risk == 0
    assert summary.candidates_rejected_risk == 1
    assert summary.trades_executed == 0
    assert summary.portfolio_cash_cents == 100_000
    assert summary.open_positions_count == 0
    assert summary.is_reconciled is True
    coord.shutdown()


# ==============================================================================
# Scenario D: Partial execution
# ==============================================================================
def test_scenario_d_partial_execution():
    """Trade with depth limit on second leg yields accurate partial fill and inventory."""
    cfg = MicrostructureConfig(
        unwind_policy=UnwindPolicy.CONSERVATIVE_LIMIT,
        sequencing_policy=LegSequencingPolicy.SIMULTANEOUS,
    )
    sim = RealisticExecutionSimulator(cfg)
    portfolio = PaperPortfolio(initial_cash_cents=100_000)

    # Leg 1: 10 contracts at 40¢ available
    # Leg 2: only 4 contracts at 35¢ available
    trade = sim.simulate_execution(
        trade_id="trade-d",
        strategy_type="binary_parity",
        target_quantity=10,
        legs_specs=[
            ("MKT-D:YES", "MKT-D", YES),
            ("MKT-D:NO", "MKT-D", NO),
        ],
        available_depth_by_leg={
            "MKT-D:YES": [(40, 10)],
            "MKT-D:NO": [(35, 4)],  # Depth limited to 4
        },
        counterparty_bids_by_leg={
            "MKT-D:YES": [(30, 10)],
            "MKT-D:NO": [(30, 10)],
        },
    )

    apply_async_trade_to_portfolio(portfolio, trade)

    assert trade.is_arbitrage_complete is False
    assert trade.legs[0].filled_quantity == 10
    assert trade.legs[1].filled_quantity == 4
    assert portfolio.reconcile().is_reconciled is True
    assert len(portfolio.positions) == 2


# ==============================================================================
# Scenario E: Unwind or scratch
# ==============================================================================
def test_scenario_e_unwind_or_scratch():
    """Legged out trade triggers emergency liquidation and updates cash proceeds cleanly."""
    cfg = MicrostructureConfig(
        unwind_policy=UnwindPolicy.EMERGENCY_DUMP,
    )
    sim = RealisticExecutionSimulator(cfg)
    portfolio = PaperPortfolio(initial_cash_cents=100_000)

    # Force Leg 2 rejection to cause LEGGED_OUT and unwind
    trade = sim.simulate_execution(
        trade_id="trade-e",
        strategy_type="binary_parity",
        target_quantity=5,
        legs_specs=[
            ("MKT-E:YES", "MKT-E", YES),
            ("MKT-E:NO", "MKT-E", NO),
        ],
        available_depth_by_leg={
            "MKT-E:YES": [(40, 5)],
            "MKT-E:NO": [(35, 5)],
        },
        counterparty_bids_by_leg={
            "MKT-E:YES": [(30, 5)],  # Bid to dump YES leg
            "MKT-E:NO": [(30, 5)],
        },
        force_scenario="LEG_2_REJECTED",
    )

    apply_async_trade_to_portfolio(portfolio, trade)

    assert trade.is_arbitrage_complete is False
    assert trade.state == MultiLegLifecycleState.CLOSED
    assert trade.unwind_proceeds_cents > 0
    assert portfolio.reconcile().is_reconciled is True
    assert portfolio.realized_pnl_cents < 0


# ==============================================================================
# Scenario F: Execution failure or timeout
# ==============================================================================
def test_scenario_f_execution_failure_resilience(tmp_path: Path):
    """Execution error does not corrupt portfolio ledger or discard existing assets."""
    state_file = tmp_path / "scenario_f.json"
    now = datetime.now(timezone.utc)
    m, b = _make_market_and_book("MKT-F", yes_bid="0.65", no_bid="0.60", timestamp=now)
    data_prov = FixtureMarketDataProvider(markets=[m], books={m.ticker: b}, current_time=now)

    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        initial_cash_cents=100_000,
        universe_config=UniverseFilterConfig(min_volume_24h=Decimal("1.00"), min_open_interest=Decimal("1.00")),
    )
    coord = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coord.startup()

    # Monkeypatch execution_manager.execute_candidate to raise a simulated transport timeout
    def mock_fail(*args, **kwargs):
        raise TimeoutError("Simulated execution gateway timeout")

    coord.execution_manager.execute_candidate = mock_fail

    summary = coord.run_single_cycle()
    assert summary.trades_executed == 0
    assert len(summary.errors) >= 1
    assert "Simulated execution gateway timeout" in summary.errors[0]
    assert coord.session_state.portfolio.reconcile().is_reconciled is True
    assert coord.session_state.portfolio.available_cash_cents == 100_000
    coord.shutdown()


# ==============================================================================
# Scenario G: Kill switch
# ==============================================================================
def test_scenario_g_kill_switch_blocking(tmp_path: Path):
    """Active kill-switch blocks new trade execution while preserving existing state."""
    state_file = tmp_path / "scenario_g.json"
    now = datetime.now(timezone.utc)
    m, b = _make_market_and_book("MKT-G", yes_bid="0.65", no_bid="0.60", timestamp=now)
    data_prov = FixtureMarketDataProvider(markets=[m], books={m.ticker: b}, current_time=now)

    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        initial_cash_cents=100_000,
        universe_config=UniverseFilterConfig(min_volume_24h=Decimal("1.00"), min_open_interest=Decimal("1.00")),
    )
    coord = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coord.startup()

    # Activate kill switch
    coord.risk_gate.activate_kill_switch("Risk operator manual trigger")
    assert coord.risk_gate.is_kill_switch_active is True

    summary = coord.run_single_cycle()
    assert summary.candidates_approved_risk == 0
    assert summary.trades_executed == 0
    assert coord.session_state.portfolio.available_cash_cents == 100_000
    coord.shutdown()


# ==============================================================================
# Scenario H: Stale or malformed market data
# ==============================================================================
def test_scenario_h_stale_or_malformed_market_data(tmp_path: Path):
    """Order books exceeding max age or future timestamp are rejected fail-closed."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(seconds=120)  # 120s old > 60s threshold
    m, b = _make_market_and_book("MKT-STALE", yes_bid="0.65", no_bid="0.60", timestamp=stale_time)
    data_prov = FixtureMarketDataProvider(markets=[m], books={m.ticker: b}, current_time=now)

    state_file = tmp_path / "scenario_h.json"
    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        initial_cash_cents=100_000,
        universe_config=UniverseFilterConfig(min_volume_24h=Decimal("1.00"), min_open_interest=Decimal("1.00")),
    )
    coord = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coord.startup()
    summary = coord.run_single_cycle()

    assert summary.markets_stale_or_invalid == 1
    assert summary.universe_qualified == 0
    assert summary.candidates_detected == 0
    assert summary.trades_executed == 0
    coord.shutdown()


# ==============================================================================
# Scenario I: Duplicate opportunities
# ==============================================================================
def test_scenario_i_duplicate_opportunities_cooldown(tmp_path: Path):
    """Deduplication and cycle cooldown suppress repeated evaluation of the same opportunity."""
    now = datetime.now(timezone.utc)
    m, b = _make_market_and_book("MKT-COOLDOWN", yes_bid="0.65", no_bid="0.60", timestamp=now)
    data_prov = FixtureMarketDataProvider(markets=[m], books={m.ticker: b}, current_time=now)

    state_file = tmp_path / "scenario_i.json"
    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        initial_cash_cents=100_000,
        universe_config=UniverseFilterConfig(min_volume_24h=Decimal("1.00"), min_open_interest=Decimal("1.00")),
    )
    coord = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coord.startup()

    # Cycle 1: Discovers and executes
    s1 = coord.run_single_cycle()
    assert s1.candidates_detected == 1
    assert s1.trades_executed == 1

    # Cycle 2: Same book presented immediately; suppressed by cooldown
    s2 = coord.run_single_cycle()
    assert s2.candidates_detected == 0
    assert s2.trades_executed == 0

    coord.shutdown()


# ==============================================================================
# Scenario J: Restart and recovery
# ==============================================================================
def test_scenario_j_restart_and_recovery(tmp_path: Path):
    """Restarting from persisted state preserves identity, cash, and positions without duplicates."""
    state_file = tmp_path / "scenario_j.json"
    now = datetime.now(timezone.utc)
    m, b = _make_market_and_book("MKT-J", yes_bid="0.65", no_bid="0.60", timestamp=now)
    data_prov = FixtureMarketDataProvider(markets=[m], books={m.ticker: b}, current_time=now)

    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        initial_cash_cents=200_000,
        universe_config=UniverseFilterConfig(min_volume_24h=Decimal("1.00"), min_open_interest=Decimal("1.00")),
    )

    # Session 1: Run 1 cycle
    c1 = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    c1.startup()
    c1.run_single_cycle()
    c1.shutdown()

    sess_id = c1.session_state.session_id
    cash_after_s1 = c1.session_state.portfolio.available_cash_cents
    pos_count_s1 = len(c1.session_state.portfolio.positions)

    # Session 2: Recover from state file
    c2 = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    c2.startup()
    assert c2.session_state.session_id == sess_id
    assert c2.session_state.portfolio.available_cash_cents == cash_after_s1
    assert len(c2.session_state.portfolio.positions) == pos_count_s1
    assert c2.session_state.portfolio.reconcile().is_reconciled is True
    assert c2.session_state.status == "RUNNING"
    c2.shutdown()
    assert c2.session_state.status == "STOPPED"


# ==============================================================================
# Scenario K: Interrupted state recovery
# ==============================================================================
def test_scenario_k_interrupted_state_recovery(tmp_path: Path):
    """Interrupted trades in EXECUTING state are safely resolved without ghost fills."""
    state_file = tmp_path / "scenario_k.json"
    store = PaperStateStore(state_file)

    # Construct interrupted session state
    state = PaperSessionState(
        session_id="interrupted-sess",
        execution_mode="paper",
        initial_cash_cents=1_000_000,
    )
    # Trade 1: Interrupted with 0 fills -> should be marked FAILED
    t1 = AsyncMultiLegTrade(
        trade_id="trade-unfilled",
        strategy_type="binary_parity",
        target_quantity=5,
        legs=(
            AsyncTradeLeg("M1", "M1", YES, intended_quantity=5, filled_quantity=0),
            AsyncTradeLeg("M1", "M1", NO, intended_quantity=5, filled_quantity=0),
        ),
        state=MultiLegLifecycleState.EXECUTING,
    )
    # Trade 2: Interrupted with partial fill (5 filled, 0 on second leg) -> should be PARTIALLY_FILLED
    t2 = AsyncMultiLegTrade(
        trade_id="trade-partial",
        strategy_type="binary_parity",
        target_quantity=5,
        legs=(
            AsyncTradeLeg("M2", "M2", YES, intended_quantity=5, filled_quantity=5),
            AsyncTradeLeg("M2", "M2", NO, intended_quantity=5, filled_quantity=0),
        ),
        state=MultiLegLifecycleState.EXECUTING,
    )
    state.trades[t1.trade_id] = t1
    state.trades[t2.trade_id] = t2
    state.portfolio._available_cash_cents = 998_000
    state.portfolio._reserved_cash_cents = 2_000  # Orphan reservation!
    store.save(state)

    # Recover
    recovered = store.load_and_recover()
    # Both trades lacked committed ledger fills, so both fail closed to prevent ghost fills
    assert recovered.trades["trade-unfilled"].state == MultiLegLifecycleState.FAILED
    assert recovered.trades["trade-partial"].state == MultiLegLifecycleState.FAILED
    # Orphan reservation released!
    assert recovered.portfolio.reserved_cash_cents == 0
    assert recovered.portfolio.reconcile().is_reconciled is True
    assert len(recovered.recovery_events) >= 2


# ==============================================================================
# Scenario L: Clean shutdown and bounded termination
# ==============================================================================
def test_scenario_l_clean_shutdown_and_bounded_termination(tmp_path: Path):
    """Bounded session terminates cleanly at max_cycles, persists STOPPED status and report."""
    state_file = tmp_path / "scenario_l.json"
    now = datetime.now(timezone.utc)
    m, b = _make_market_and_book("MKT-L", yes_bid="0.45", no_bid="0.45", timestamp=now)
    data_prov = FixtureMarketDataProvider(markets=[m], books={m.ticker: b}, current_time=now)

    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        max_cycles=3,
        initial_cash_cents=100_000,
        universe_config=UniverseFilterConfig(min_volume_24h=Decimal("1.00"), min_open_interest=Decimal("1.00")),
    )
    coord = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coord.run_loop()

    assert coord.runtime_state == CoordinatorState.STOPPED
    assert coord._cycle_count == 3
    assert coord.session_state.last_completed_cycle == 3
    assert coord.session_state.status == "STOPPED"

    report = coord.generate_session_report()
    assert report["total_cycles_completed"] == 3
    assert report["execution_mode"] == "paper"
    assert report["is_reconciled"] is True
