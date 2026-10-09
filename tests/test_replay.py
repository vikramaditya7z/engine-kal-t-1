from decimal import Decimal
import pytest

from kalshi_arbitrage import (
    EVENT_TYPE_OPPORTUNITY,
    EVENT_TYPE_SETTLEMENT,
    NO,
    NormalizedOrderBook,
    OPPORTUNITY_BINARY_PARITY,
    OrderBookLevel,
    ReplayEngine,
    ReplayEvent,
    RiskConfig,
    TradeState,
    YES,
)


def make_level(price_str: str, qty_str: str) -> OrderBookLevel:
    return OrderBookLevel(price_dollars=Decimal(price_str), quantity=Decimal(qty_str))


def make_book(ticker: str = "KXTEST", yes_bids=(), no_bids=()) -> NormalizedOrderBook:
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=tuple(make_level(p, q) for p, q in yes_bids),
        no_bids=tuple(make_level(p, q) for p, q in no_bids),
    )


def test_replay_chronological_ordering_and_deterministic_output():
    engine = ReplayEngine(default_fee_per_contract_cents=1)

    book1 = make_book("M1", yes_bids=[("0.4500", "10.00")], no_bids=[("0.6000", "10.00")])  # 95¢ cost, 5¢ edge
    book2 = make_book("M2", yes_bids=[("0.4000", "10.00")], no_bids=[("0.6500", "10.00")])  # 95¢ cost, 5¢ edge

    ev1 = ReplayEvent(
        timestamp="2026-10-09T10:00:00Z",
        event_type=EVENT_TYPE_OPPORTUNITY,
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        book=book1,
        requested_quantity=5,
    )
    ev2 = ReplayEvent(
        timestamp="2026-10-09T11:00:00Z",
        event_type=EVENT_TYPE_OPPORTUNITY,
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        book=book2,
        requested_quantity=5,
    )

    # Pass in reverse order: engine must sort chronologically!
    report1 = engine.run([ev2, ev1], initial_cash_cents=20_000)
    report2 = engine.run([ev1, ev2], initial_cash_cents=20_000)

    # Results must be 100% identical
    assert report1.metrics == report2.metrics
    assert report1.equity_curve == report2.equity_curve
    assert report1.metrics.trades_accepted == 2
    assert report1.metrics.trades_filled == 2
    assert report1.metrics.opportunities_evaluated == 2
    assert report1.metrics.total_cost_cents == 475 * 2  # 950¢
    assert report1.metrics.total_fees_cents == 20  # 10 contracts per trade * 1¢ = 20¢


def test_replay_with_settlement_and_drawdown():
    engine = ReplayEngine(default_fee_per_contract_cents=2)

    # Profitable book
    book = make_book("M1", yes_bids=[("0.4500", "10.00")], no_bids=[("0.6000", "10.00")])

    ev_trade = ReplayEvent(
        timestamp="2026-10-09T10:00:00Z",
        event_type=EVENT_TYPE_OPPORTUNITY,
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        book=book,
        requested_quantity=4,
    )
    ev_settle = ReplayEvent(
        timestamp="2026-10-09T12:00:00Z",
        event_type=EVENT_TYPE_SETTLEMENT,
        settle_market_ticker="M1",
        settle_winning_side=YES,
    )

    report = engine.run([ev_trade, ev_settle], initial_cash_cents=10_000)

    m = report.metrics
    assert m.trades_filled == 1
    # Cost = 4 * 95 = 380¢. Fees = 8 contracts * 2¢ = 16¢. Total cash out = 396¢.
    # Settlement payout = 4 * 100 = 400¢.
    # Realized P&L = 400 - 380 = +20¢.
    # Final cash = 10,000 - 396 + 400 = 10,004¢.
    # Net profit = +4¢ (20¢ gross edge - 16¢ fees).
    assert m.final_cash_cents == 10_004
    assert m.realized_pnl_cents == 20
    assert m.net_profit_cents == 4
    assert m.total_fees_cents == 16
    assert len(report.portfolio.positions) == 0


def test_replay_rejections_tracking():
    # Tight risk limit
    cfg = RiskConfig(max_cost_per_trade_cents=200)
    engine = ReplayEngine(risk_config=cfg)

    # Book has cost 95¢/pair. Request 5 pairs -> cost 475¢ > 200¢ -> Rejected by risk!
    book = make_book("M1", yes_bids=[("0.4500", "10.00")], no_bids=[("0.6000", "10.00")])
    ev1 = ReplayEvent(
        timestamp="2026-10-09T10:00:00Z",
        event_type=EVENT_TYPE_OPPORTUNITY,
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        book=book,
        requested_quantity=5,
    )

    # Empty book -> Rejected by depth!
    empty_book = make_book("M2", yes_bids=[], no_bids=[])
    ev2 = ReplayEvent(
        timestamp="2026-10-09T11:00:00Z",
        event_type=EVENT_TYPE_OPPORTUNITY,
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        book=empty_book,
        requested_quantity=1,
    )

    report = engine.run([ev1, ev2], initial_cash_cents=10_000)

    assert report.metrics.opportunities_evaluated == 2
    assert report.metrics.opportunities_rejected_risk == 1
    assert report.metrics.opportunities_rejected_depth == 1
    assert report.metrics.trades_accepted == 0
    assert len(report.metrics.rejection_reasons) == 2


def test_replay_report_summary_formatting():
    engine = ReplayEngine()
    book = make_book("M1", yes_bids=[("0.4500", "5.00")], no_bids=[("0.6000", "5.00")])
    ev = ReplayEvent(
        timestamp="2026-10-09T10:00:00Z",
        event_type=EVENT_TYPE_OPPORTUNITY,
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        book=book,
        requested_quantity=2,
    )

    report = engine.run([ev], initial_cash_cents=10_000)
    summary_text = report.summary()

    assert "HISTORICAL REPLAY REPORT" in summary_text
    assert "Initial Cash:" in summary_text
    assert "Final Equity:" in summary_text
    assert "Equity Model:" in summary_text
    assert "ACCOUNTING POLICY" in summary_text
    assert "DISCLAIMER" in summary_text


def test_replay_book_value_equity_curve_and_drawdown_progression():
    engine = ReplayEngine(default_fee_per_contract_cents=1)
    book = make_book("M1", yes_bids=[("0.4500", "10.00")], no_bids=[("0.6000", "10.00")])

    t_trade = "2026-10-09T10:00:00Z"
    t_settle = "2026-10-09T12:00:00Z"

    ev_trade = ReplayEvent(
        timestamp=t_trade,
        event_type=EVENT_TYPE_OPPORTUNITY,
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        book=book,
        requested_quantity=5,  # 5 pairs * 95¢ = 475¢ cost; 10 contracts * 1¢ = 10¢ fee
    )
    ev_settle = ReplayEvent(
        timestamp=t_settle,
        event_type=EVENT_TYPE_SETTLEMENT,
        settle_market_ticker="M1",
        settle_winning_side=YES,
    )

    report = engine.run([ev_trade, ev_settle], initial_cash_cents=10_000)

    # Verify equity curve points:
    # Point 0: START -> 10,000¢
    # Point 1: Trade filled -> Cash = 9,515¢, Cost basis = 475¢ -> Equity = 9,990¢ (drawdown = 10¢ fee)
    # Point 2: Settled -> Cash = 9,515 + 500 = 10,015¢, Cost basis = 0¢ -> Equity = 10,015¢
    assert report.equity_curve == (
        ("START", 10_000),
        (t_trade, 9_990),
        (t_settle, 10_015),
    )

    m = report.metrics
    assert m.max_drawdown_cents == 10  # 10¢ drawdown during open position due to fee
    assert m.max_drawdown_pct == (Decimal(10) / Decimal(10_015)) * Decimal(100)
    assert m.final_cash_cents == 10_015
    assert m.final_equity_cents == 10_015
    assert m.net_profit_cents == 15
    assert m.realized_pnl_cents == 25  # 500¢ payout - 475¢ cost basis
    assert m.total_fees_cents == 10
    assert m.unrealized_pnl_cents == 0

