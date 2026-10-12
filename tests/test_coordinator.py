"""Tests for V9 Autonomous Paper Trading Coordinator."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import pytest

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
from kalshi_arbitrage.paper_risk import PaperRiskConfig
from kalshi_arbitrage.universe import UniverseFilterConfig


def _create_fixture_provider() -> FixtureMarketDataProvider:
    now = datetime.now(timezone.utc)
    # Market with profitable parity
    m1 = NormalizedMarket(
        ticker="MKT-ARB-1",
        event_ticker="EVENT-1",
        market_type="binary",
        status="active",
        title="Arb Market 1",
        yes_subtitle="Yes",
        no_subtitle="No",
        volume_24h=Decimal("50.00"),
        open_interest=Decimal("100.00"),
        yes_bid_dollars=Decimal("0.65"),
        no_bid_dollars=Decimal("0.60"),
    )
    b1 = NormalizedOrderBook(
        market_ticker="MKT-ARB-1",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.65"), quantity=Decimal("10")),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.60"), quantity=Decimal("10")),),
        source_timestamp=now,
    )
    # Market with no edge
    m2 = NormalizedMarket(
        ticker="MKT-NO-EDGE",
        event_ticker="EVENT-2",
        market_type="binary",
        status="active",
        title="No Edge Market",
        yes_subtitle="Yes",
        no_subtitle="No",
        volume_24h=Decimal("20.00"),
        open_interest=Decimal("50.00"),
        yes_bid_dollars=Decimal("0.40"),
        no_bid_dollars=Decimal("0.40"),
    )
    b2 = NormalizedOrderBook(
        market_ticker="MKT-NO-EDGE",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.40"), quantity=Decimal("10")),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.40"), quantity=Decimal("10")),),
        source_timestamp=now,
    )
    return FixtureMarketDataProvider(
        markets=[m1, m2],
        books={"MKT-ARB-1": b1, "MKT-NO-EDGE": b2},
        current_time=now,
    )


def test_coordinator_rejects_non_paper_mode(tmp_path: Path):
    with pytest.raises(CoordinatorError) as exc:
        PaperCoordinatorConfig(
            execution_mode="live",
            state_filepath=tmp_path / "state.json",
        )
    assert "requires execution_mode='paper'" in str(exc.value)


def test_coordinator_startup_and_single_cycle(tmp_path: Path):
    state_file = tmp_path / "coordinator_state.json"
    data_prov = _create_fixture_provider()

    # Configure universe to accept our arb market
    u_cfg = UniverseFilterConfig(
        min_volume_24h=Decimal("5.00"),
        min_open_interest=Decimal("5.00"),
        max_spread_cents=20,
    )

    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        discovery_refresh_interval_seconds=10.0,
        initial_cash_cents=100_000,
        universe_config=u_cfg,
    )

    coord = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    assert coord.runtime_state == CoordinatorState.STOPPED

    coord.startup()
    assert coord.runtime_state == CoordinatorState.RUNNING
    assert coord.session_state is not None
    assert coord.session_state.portfolio.available_cash_cents == 100_000
    assert state_file.exists()

    summary = coord.run_single_cycle()
    assert summary.cycle_index == 1
    assert summary.markets_discovered == 2
    assert summary.universe_qualified >= 1
    assert summary.candidates_detected >= 1
    assert summary.candidates_approved_risk >= 1
    assert summary.trades_executed == 1
    assert summary.is_reconciled is True
    assert len(summary.errors) == 0

    # Ensure persistence was updated
    assert len(coord.session_state.trades) == 1
    assert coord.session_state.last_completed_cycle == 1

    coord.shutdown()
    assert coord.runtime_state == CoordinatorState.STOPPED


def test_coordinator_bounded_run_loop(tmp_path: Path):
    state_file = tmp_path / "bounded_state.json"
    data_prov = _create_fixture_provider()

    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        discovery_refresh_interval_seconds=5.0,
        max_cycles=3,  # Run exactly 3 cycles
        initial_cash_cents=100_000,
        universe_config=UniverseFilterConfig(min_volume_24h=Decimal("1.00"), min_open_interest=Decimal("1.00")),
    )

    coord = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coord.run_loop()

    assert coord.runtime_state == CoordinatorState.STOPPED
    assert coord._cycle_count == 3
    assert len(coord.cycle_summaries) == 3

    report = coord.generate_session_report()
    assert report["total_cycles_completed"] == 3
    assert report["execution_mode"] == "paper"
    assert report["is_reconciled"] is True
    assert len(report["discrepancies"]) == 0


def test_coordinator_restart_recovery(tmp_path: Path):
    state_file = tmp_path / "restart_state.json"
    data_prov = _create_fixture_provider()

    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        discovery_refresh_interval_seconds=5.0,
        max_cycles=1,
        initial_cash_cents=100_000,
        universe_config=UniverseFilterConfig(min_volume_24h=Decimal("1.00"), min_open_interest=Decimal("1.00")),
    )

    # 1. Run first coordinator instance for 1 cycle
    coord1 = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coord1.run_loop()
    sess_id = coord1.session_state.session_id
    trades_count = len(coord1.session_state.trades)

    # 2. Start a second coordinator instance pointing to the same state file
    coord2 = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coord2.startup()

    assert coord2.session_state.session_id == sess_id
    assert len(coord2.session_state.trades) == trades_count
    assert coord2.session_state.last_completed_cycle == 1
    assert coord2.session_state.portfolio.reconcile().is_reconciled is True

    coord2.shutdown()
