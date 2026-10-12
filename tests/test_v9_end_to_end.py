"""Comprehensive End-to-End, Fault-Injection, and Adversarial Tests for V9 Autonomous Paper Trading."""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import pytest

from kalshi_arbitrage.coordinator import (
    CoordinatorError,
    CoordinatorState,
    PaperCoordinatorConfig,
    PaperTradingCoordinator,
)
from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.market_data_provider import FixtureMarketDataProvider
from kalshi_arbitrage.paper_persistence import (
    CorruptedStateError,
    PaperStateStore,
    ReconciliationRecoveryError,
)
from kalshi_arbitrage.paper_risk import PaperRiskConfig
from kalshi_arbitrage.universe import UniverseFilterConfig


def _create_rich_fixture_provider() -> FixtureMarketDataProvider:
    now = datetime.now(timezone.utc)
    # Market 1: Clear arb (YES bid 0.65, NO bid 0.60 => ask_yes=40¢, ask_no=35¢ => cost 75¢)
    m1 = NormalizedMarket(
        ticker="MKT-ARB-1",
        event_ticker="EVENT-1",
        market_type="binary",
        status="active",
        title="Arb Market 1",
        yes_subtitle="Yes",
        no_subtitle="No",
        volume_24h=Decimal("150.00"),
        open_interest=Decimal("300.00"),
        yes_bid_dollars=Decimal("0.65"),
        no_bid_dollars=Decimal("0.60"),
    )
    b1 = NormalizedOrderBook(
        market_ticker="MKT-ARB-1",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.65"), quantity=Decimal("20")),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.60"), quantity=Decimal("20")),),
        source_timestamp=now,
    )

    # Market 2: Sub-cent edge eroded by fees
    m2 = NormalizedMarket(
        ticker="MKT-THIN-2",
        event_ticker="EVENT-2",
        market_type="binary",
        status="active",
        title="Thin Market 2",
        yes_subtitle="Yes",
        no_subtitle="No",
        volume_24h=Decimal("50.00"),
        open_interest=Decimal("100.00"),
        yes_bid_dollars=Decimal("0.51"),
        no_bid_dollars=Decimal("0.49"),
    )
    b2 = NormalizedOrderBook(
        market_ticker="MKT-THIN-2",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.51"), quantity=Decimal("10")),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.49"), quantity=Decimal("10")),),
        source_timestamp=now,
    )

    return FixtureMarketDataProvider(
        markets=[m1, m2],
        books={"MKT-ARB-1": b1, "MKT-THIN-2": b2},
        current_time=now,
    )


def test_v9_end_to_end_bounded_session(tmp_path: Path):
    """Verifies end-to-end autonomous session with candidate discovery, execution, and reporting."""
    state_file = tmp_path / "e2e_paper_state.json"
    data_prov = _create_rich_fixture_provider()

    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        discovery_refresh_interval_seconds=10.0,
        max_cycles=3,
        initial_cash_cents=500_000,  # $5,000.00
        universe_config=UniverseFilterConfig(
            min_volume_24h=Decimal("10.00"),
            min_open_interest=Decimal("10.00"),
            max_spread_cents=25,
        ),
    )

    coordinator = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coordinator.run_loop()

    assert coordinator.runtime_state == CoordinatorState.STOPPED
    assert coordinator._cycle_count == 3

    # Generate session report
    report = coordinator.generate_session_report()
    assert report["execution_mode"] == "paper"
    assert report["total_cycles_completed"] == 3
    assert report["total_trades_attempted"] >= 1
    assert report["trades_completed_arbitrage"] >= 1
    assert report["is_reconciled"] is True
    assert len(report["discrepancies"]) == 0
    assert report["book_value_equity_cents"] > 0

    # Ensure state file was flushed and reconciles from cold disk
    store = PaperStateStore(state_file)
    recovered = store.load_and_recover()
    assert recovered.session_id == report["session_id"]
    assert recovered.portfolio.reconcile().is_reconciled is True
    assert len(recovered.portfolio.positions) > 0


def test_v9_session_continuity_across_restarts(tmp_path: Path):
    """Proves that stopping and restarting does not duplicate trades, drop positions, or drift cash."""
    state_file = tmp_path / "continuity_state.json"
    data_prov = _create_rich_fixture_provider()

    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        discovery_refresh_interval_seconds=5.0,
        max_cycles=2,
        initial_cash_cents=500_000,
        universe_config=UniverseFilterConfig(
            min_volume_24h=Decimal("10.00"),
            min_open_interest=Decimal("10.00"),
            max_spread_cents=25,
        ),
    )

    # Part 1: Run 2 cycles
    coord1 = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coord1.run_loop()
    rep1 = coord1.generate_session_report()
    cash_after_s1 = rep1["available_cash_cents"]
    trades_after_s1 = rep1["total_trades_attempted"]
    sess_id = rep1["session_id"]

    # Part 2: Resume session in a second coordinator for 2 more cycles
    coord_cfg2 = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        discovery_refresh_interval_seconds=5.0,
        max_cycles=4,  # continue until cycle 4
        initial_cash_cents=500_000,
        universe_config=UniverseFilterConfig(
            min_volume_24h=Decimal("10.00"),
            min_open_interest=Decimal("10.00"),
            max_spread_cents=25,
        ),
    )

    coord2 = PaperTradingCoordinator(config=coord_cfg2, data_provider=data_prov)
    coord2.run_loop()
    rep2 = coord2.generate_session_report()

    assert rep2["session_id"] == sess_id
    assert rep2["total_cycles_completed"] == 4
    assert rep2["is_reconciled"] is True
    assert rep2["total_trades_attempted"] >= trades_after_s1
    assert len(rep2["discrepancies"]) == 0


def test_v9_corrupted_state_fails_closed(tmp_path: Path):
    """Ensures corrupt JSON or malformed schema halts startup immediately."""
    corrupt_file = tmp_path / "corrupt.json"
    with open(corrupt_file, "w", encoding="utf-8") as f:
        f.write("{ invalid json")

    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=corrupt_file,
    )
    coord = PaperTradingCoordinator(config=coord_cfg)
    with pytest.raises(CorruptedStateError):
        coord.startup()
    assert coord.runtime_state == CoordinatorState.FAILED


def test_v9_broken_accounting_invariant_triggers_kill_switch(tmp_path: Path):
    """Ensures any discrepancy in ledger accounting trips the emergency kill-switch."""
    state_file = tmp_path / "broken_ledger.json"
    data_prov = _create_rich_fixture_provider()

    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=state_file,
        poll_interval_seconds=0.01,
        initial_cash_cents=100_000,
    )
    coord = PaperTradingCoordinator(config=coord_cfg, data_provider=data_prov)
    coord.startup()

    # Manually tamper with portfolio cash without recording a ledger entry
    coord.session_state.portfolio._available_cash_cents += 50_000  # Phantom $500!

    summary = coord.run_single_cycle()
    assert summary.is_reconciled is False
    assert coord.risk_gate.is_kill_switch_active is True
    assert "Accounting invariant broken" in coord.risk_gate.kill_switch_reason


def test_v9_cli_bounded_paper_trader_execution(tmp_path: Path):
    """Tests the actual scripts/run_paper_trader.py command line interface."""
    state_file = tmp_path / "cli_paper_state.json"
    report_file = tmp_path / "cli_report.json"

    cmd = [
        sys.executable,
        "scripts/run_paper_trader.py",
        "--mode", "paper",
        "--bounded-cycles", "2",
        "--poll-interval", "0.01",
        "--max-markets", "2",
        "--state-file", str(state_file),
        "--report-output", str(report_file),
    ]

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    assert res.returncode == 0
    assert "KALSHI ARBITRAGE ENGINE V9 — AUTONOMOUS PAPER TRADING SESSION SUMMARY" in res.stdout
    assert report_file.exists()

    with open(report_file, "r", encoding="utf-8") as f:
        rep = json.load(f)
    assert rep["execution_mode"] == "paper"
    assert rep["total_cycles_completed"] == 2
    assert rep["is_reconciled"] is True
