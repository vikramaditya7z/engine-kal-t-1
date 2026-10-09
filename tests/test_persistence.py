from decimal import Decimal
import json
import pytest

from kalshi_arbitrage import (
    CorruptedStateError,
    NO,
    PaperPortfolio,
    PersistenceError,
    ReconciliationRecoveryError,
    RiskConfig,
    TradeLeg,
    TradeState,
    YES,
    create_proposed_trade,
    load_state,
    save_state,
)


def make_test_trade(trade_id: str = "T1", qty: int = 5, cost_cents: int = 450, fee_cents: int = 10):
    trade = create_proposed_trade(trade_id=trade_id, strategy_type="BINARY_PARITY", requested_quantity=qty)
    leg_yes = TradeLeg(
        contract_id="KX1",
        market_ticker="KX1",
        side=YES,
        requested_quantity=qty,
        filled_quantity=qty,
        total_cost_cents=cost_cents // 2,
        total_cost_dollars=Decimal(cost_cents // 2) / Decimal(100),
        average_price_cents=Decimal(cost_cents // 2) / Decimal(qty),
        average_price_dollars=Decimal(cost_cents // 2) / Decimal(100) / Decimal(qty),
        consumed_levels=(),
    )
    leg_no = TradeLeg(
        contract_id="KX1",
        market_ticker="KX1",
        side=NO,
        requested_quantity=qty,
        filled_quantity=qty,
        total_cost_cents=cost_cents // 2,
        total_cost_dollars=Decimal(cost_cents // 2) / Decimal(100),
        average_price_cents=Decimal(cost_cents // 2) / Decimal(qty),
        average_price_dollars=Decimal(cost_cents // 2) / Decimal(100) / Decimal(qty),
        consumed_levels=(),
    )
    return trade.transition_to(
        TradeState.ACCEPTED
    ).transition_to(
        TradeState.FILLED,
        filled_quantity=qty,
        total_cost_cents=cost_cents,
        estimated_fees_cents=fee_cents,
        guaranteed_payout_cents=qty * 100,
        legs=(leg_yes, leg_no),
    )


def test_persistence_save_and_load_round_trip(tmp_path):
    save_file = tmp_path / "state" / "portfolio.json"

    p = PaperPortfolio(initial_cash_cents=20_000)
    t1 = make_test_trade("T1", qty=4, cost_cents=360, fee_cents=8)
    p.reserve_for_trade(t1)
    p.apply_trade_fill(t1)

    cfg = RiskConfig(max_cost_per_trade_cents=40_000, max_requested_quantity=500)

    # Save
    save_state(save_file, p, trades=[t1], risk_config=cfg)
    assert save_file.exists()

    # Load
    loaded_p, loaded_trades, loaded_cfg = load_state(save_file)

    assert loaded_p.available_cash_cents == p.available_cash_cents
    assert loaded_p.reserved_cash_cents == p.reserved_cash_cents
    assert loaded_p.total_fees_paid_cents == p.total_fees_paid_cents
    assert len(loaded_p.positions) == len(p.positions)
    assert len(loaded_p.ledger) == len(p.ledger)
    assert len(loaded_trades) == 1
    assert loaded_trades[0].trade_id == "T1"
    assert loaded_trades[0].state == TradeState.FILLED
    assert loaded_cfg.max_cost_per_trade_cents == 40_000

    # Ensure loaded portfolio is reconciled!
    rec = loaded_p.reconcile()
    assert rec.is_reconciled is True


def test_persistence_reconciliation_failure_fails_closed(tmp_path):
    save_file = tmp_path / "state.json"
    p = PaperPortfolio(initial_cash_cents=10_000)
    t1 = make_test_trade("T1")
    p.apply_trade_fill(t1)
    save_state(save_file, p, [t1])

    # Tamper with file to introduce financial discrepancy
    with open(save_file, "r") as f:
        data = json.load(f)

    # Corrupt available cash in portfolio section without updating ledger
    data["portfolio"]["available_cash_cents"] += 9999

    with open(save_file, "w") as f:
        json.dump(data, f)

    # Loading must fail closed via ReconciliationRecoveryError
    with pytest.raises(ReconciliationRecoveryError, match="State recovery failed audit reconciliation"):
        load_state(save_file)


def test_persistence_incompatible_version_rejected(tmp_path):
    save_file = tmp_path / "state.json"
    p = PaperPortfolio(initial_cash_cents=10_000)
    save_state(save_file, p, [])

    with open(save_file, "r") as f:
        data = json.load(f)
    data["schema_version"] = "99.0"
    with open(save_file, "w") as f:
        json.dump(data, f)

    with pytest.raises(CorruptedStateError, match="Unsupported or missing schema version"):
        load_state(save_file)


def test_persistence_missing_sections_rejected(tmp_path):
    save_file = tmp_path / "state.json"
    p = PaperPortfolio(initial_cash_cents=10_000)
    save_state(save_file, p, [])

    with open(save_file, "r") as f:
        data = json.load(f)
    del data["ledger"]
    with open(save_file, "w") as f:
        json.dump(data, f)

    with pytest.raises(CorruptedStateError, match="Missing required top-level section: 'ledger'"):
        load_state(save_file)


def test_persistence_missing_file_raises_persistence_error(tmp_path):
    with pytest.raises(PersistenceError, match="does not exist"):
        load_state(tmp_path / "nonexistent.json")


def test_persistence_deleted_filled_trade_fails_reconciliation(tmp_path):
    save_file = tmp_path / "state.json"
    p = PaperPortfolio(initial_cash_cents=10_000)
    t1 = make_test_trade("T1")
    p.apply_trade_fill(t1)
    save_state(save_file, p, [t1])

    # Delete trade from trades list in JSON file
    with open(save_file, "r") as f:
        data = json.load(f)
    data["trades"] = []  # Omit trade T1
    with open(save_file, "w") as f:
        json.dump(data, f)

    with pytest.raises(ReconciliationRecoveryError, match="missing from trades"):
        load_state(save_file)


def test_persistence_altered_trade_amount_fails_reconciliation(tmp_path):
    save_file = tmp_path / "state.json"
    p = PaperPortfolio(initial_cash_cents=10_000)
    t1 = make_test_trade("T1", qty=5, cost_cents=450, fee_cents=10)
    p.apply_trade_fill(t1)
    save_state(save_file, p, [t1])

    # Alter trade total cost in trades section
    with open(save_file, "r") as f:
        data = json.load(f)
    data["trades"][0]["total_cost_cents"] = 999
    with open(save_file, "w") as f:
        json.dump(data, f)

    with pytest.raises(ReconciliationRecoveryError, match="does not match ledger positions cost"):
        load_state(save_file)


def test_persistence_inconsistent_trade_state_fails_reconciliation(tmp_path):
    save_file = tmp_path / "state.json"
    p = PaperPortfolio(initial_cash_cents=10_000)
    t1 = make_test_trade("T1")
    p.apply_trade_fill(t1)
    save_state(save_file, p, [t1])

    # Alter trade state in trades section to REJECTED while ledger has FILL
    with open(save_file, "r") as f:
        data = json.load(f)
    data["trades"][0]["state"] = "REJECTED"
    with open(save_file, "w") as f:
        json.dump(data, f)

    with pytest.raises(ReconciliationRecoveryError, match="has state REJECTED in trades record, but has a FILL entry"):
        load_state(save_file)


def test_persistence_preserves_legitimate_rejected_trades(tmp_path):
    save_file = tmp_path / "state.json"
    p = PaperPortfolio(initial_cash_cents=10_000)
    t1 = make_test_trade("T1")
    p.apply_trade_fill(t1)

    t2 = create_proposed_trade(trade_id="T2_REJ", strategy_type="BINARY_PARITY", requested_quantity=5)
    t2_rejected = t2.transition_to(TradeState.REJECTED, reason="Risk limit exceeded")

    save_state(save_file, p, [t1, t2_rejected])

    loaded_p, loaded_trades, _ = load_state(save_file)
    assert len(loaded_trades) == 2
    assert loaded_trades[0].trade_id == "T1"
    assert loaded_trades[1].trade_id == "T2_REJ"
    assert loaded_trades[1].state == TradeState.REJECTED
    assert loaded_p.reconcile().is_reconciled is True

