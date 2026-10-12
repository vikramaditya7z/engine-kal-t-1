"""Tests for V9 Paper State Store, atomic persistence, and crash recovery."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import pytest

from kalshi_arbitrage.async_execution import (
    AsyncMultiLegTrade,
    AsyncTradeLeg,
    MultiLegLifecycleState,
)
from kalshi_arbitrage.contract import NO, YES
from kalshi_arbitrage.ledger import (
    LedgerEntry,
    LedgerEntryType,
    PaperPortfolio,
    PositionDelta,
    PositionHolding,
)
from kalshi_arbitrage.paper_persistence import (
    CorruptedStateError,
    PAPER_SCHEMA_VERSION,
    PaperSessionState,
    PaperStateStore,
    ReconciliationRecoveryError,
)
from kalshi_arbitrage.paper_risk import PaperRiskConfig


def test_round_trip_save_and_load(tmp_path: Path):
    filepath = tmp_path / "paper_state.json"
    store = PaperStateStore(filepath)

    portfolio = PaperPortfolio(initial_cash_cents=100_000)
    # Add a position and ledger entry (cost 5000 + fees 200 = 5200)
    portfolio._available_cash_cents -= 5_200
    portfolio._total_fees_cents += 200
    portfolio._positions[("MKT-1", YES)] = PositionHolding.create("MKT-1", YES, 10, 5_000)
    entry = LedgerEntry(
        entry_id="ent-1",
        entry_type=LedgerEntryType.FILL,
        timestamp="2026-10-12T12:00:00Z",
        trade_id="trd-1",
        cash_delta_cents=-5_200,
        reserved_delta_cents=0,
        fees_delta_cents=200,
        realized_pnl_delta_cents=0,
        positions_delta=(
            PositionDelta(contract_id="MKT-1", side=YES, quantity_delta=10, cost_delta_cents=5_000),
        ),
        balance_after_cents=94_800,
        reserved_after_cents=0,
        description="Fill simulated",
    )
    portfolio._ledger.append(entry)

    leg1 = AsyncTradeLeg.create("MKT-1", "MKT-1", YES, 10).with_fill(
        filled_qty=10, cost_cents=5_000, fees_cents=200, fill_time=datetime.now(timezone.utc)
    )
    trade = AsyncMultiLegTrade(
        trade_id="trd-1",
        strategy_type="BINARY_PARITY",
        target_quantity=10,
        legs=(leg1,),
        state=MultiLegLifecycleState.CLOSED,
        total_cost_cents=5_000,
        total_fees_cents=200,
    )

    state = PaperSessionState(
        session_id="test-session-1",
        execution_mode="paper",
        initial_cash_cents=100_000,
        portfolio=portfolio,
        trades={"trd-1": trade},
    )

    store.save(state)
    assert filepath.exists()

    recovered = store.load_and_recover()
    assert recovered.session_id == "test-session-1"
    assert recovered.execution_mode == "paper"
    assert recovered.portfolio.available_cash_cents == 94_800
    assert recovered.portfolio.total_fees_paid_cents == 200
    assert ("MKT-1", YES) in recovered.portfolio.positions
    assert len(recovered.trades) == 1
    assert recovered.trades["trd-1"].state == MultiLegLifecycleState.CLOSED


def test_rejects_non_paper_execution_mode(tmp_path: Path):
    filepath = tmp_path / "live_state.json"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write('{"schema_version": "2.0", "execution_mode": "live", "portfolio": {}}')

    store = PaperStateStore(filepath)
    with pytest.raises(CorruptedStateError) as exc:
        store.load_and_recover()
    assert "Must be 'paper'" in str(exc.value)


def test_interrupted_trade_recovery_no_fills_marks_failed(tmp_path: Path):
    filepath = tmp_path / "interrupted_state.json"
    store = PaperStateStore(filepath)

    portfolio = PaperPortfolio(initial_cash_cents=100_000)
    # Trade was EXECUTING but crashed before fills were committed to ledger
    leg1 = AsyncTradeLeg.create("MKT-1", "MKT-1", YES, 10)
    trade = AsyncMultiLegTrade(
        trade_id="trd-crash",
        strategy_type="BINARY_PARITY",
        target_quantity=10,
        legs=(leg1,),
        state=MultiLegLifecycleState.EXECUTING,
    )

    state = PaperSessionState(
        session_id="crash-session",
        execution_mode="paper",
        portfolio=portfolio,
        trades={"trd-crash": trade},
    )
    store.save(state)

    recovered = store.load_and_recover()
    assert recovered.trades["trd-crash"].state == MultiLegLifecycleState.FAILED
    assert len(recovered.recovery_events) > 0
    assert "TRANSITION_TO_FAILED" in recovered.recovery_events[0]["action"]


def test_interrupted_trade_with_fills_preserves_holdings(tmp_path: Path):
    filepath = tmp_path / "interrupted_partial.json"
    store = PaperStateStore(filepath)

    portfolio = PaperPortfolio(initial_cash_cents=100_000)
    portfolio._available_cash_cents -= 3_000
    portfolio._positions[("MKT-1", YES)] = PositionHolding.create("MKT-1", YES, 5, 3_000)
    entry = LedgerEntry(
        entry_id="ent-1",
        entry_type=LedgerEntryType.FILL,
        timestamp="2026-10-12T12:00:00Z",
        trade_id="trd-partial",
        cash_delta_cents=-3_000,
        reserved_delta_cents=0,
        fees_delta_cents=0,
        realized_pnl_delta_cents=0,
        positions_delta=(
            PositionDelta(contract_id="MKT-1", side=YES, quantity_delta=5, cost_delta_cents=3_000),
        ),
        balance_after_cents=97_000,
        reserved_after_cents=0,
        description="Partial fill",
    )
    portfolio._ledger.append(entry)

    leg1 = AsyncTradeLeg.create("MKT-1", "MKT-1", YES, 10).with_fill(
        filled_qty=5, cost_cents=3_000, fees_cents=0, fill_time=datetime.now(timezone.utc)
    )
    trade = AsyncMultiLegTrade(
        trade_id="trd-partial",
        strategy_type="BINARY_PARITY",
        target_quantity=10,
        legs=(leg1,),
        state=MultiLegLifecycleState.EXECUTING,
        total_cost_cents=3_000,
    )

    state = PaperSessionState(
        session_id="partial-session",
        execution_mode="paper",
        portfolio=portfolio,
        trades={"trd-partial": trade},
    )
    store.save(state)

    recovered = store.load_and_recover()
    assert recovered.trades["trd-partial"].state == MultiLegLifecycleState.PARTIALLY_FILLED
    assert ("MKT-1", YES) in recovered.portfolio.positions
    assert recovered.portfolio.positions[("MKT-1", YES)].quantity == 5


def test_orphan_reservation_released_on_restart(tmp_path: Path):
    filepath = tmp_path / "orphan_res.json"
    store = PaperStateStore(filepath)

    portfolio = PaperPortfolio(initial_cash_cents=100_000)
    # 5000¢ was reserved when process crashed
    portfolio._available_cash_cents = 95_000
    portfolio._reserved_cash_cents = 5_000

    state = PaperSessionState(
        session_id="orphan-session",
        execution_mode="paper",
        portfolio=portfolio,
        trades={},
    )
    store.save(state)

    recovered = store.load_and_recover()
    # Reservation should be cleanly unlocked back to available cash
    assert recovered.portfolio.reserved_cash_cents == 0
    assert recovered.portfolio.available_cash_cents == 100_000
    assert any(e.get("action") == "RELEASE_ORPHAN_RESERVATION" for e in recovered.recovery_events)
