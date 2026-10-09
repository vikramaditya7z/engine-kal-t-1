"""Comprehensive End-to-End Integration Tests for V5 Live Market Observer.

Verifies the complete data-flow pipeline:
Mock transport / public market data
-> normalization
-> freshness validation
-> V2 candidate detection
-> V3 execution pricing
-> evidence storage
-> optional V4 paper trading & deduplication
-> ledger reconciliation
-> offline historical replay
-> metrics and reporting.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

from kalshi_arbitrage import (
    DeclaredMeceBasket,
    EvidenceStore,
    HTTPResponse,
    KalshiHTTPError,
    KalshiRestClient,
    KalshiTransportError,
    MarketObserver,
    MarketObserverConfig,
    PaperPortfolio,
    RiskConfig,
    RiskManager,
    STATUS_QUALIFIED,
    TradeState,
    YES,
    format_http_date,
    replay_recorded_evidence,
)


class MockTransport:
    """Deterministic scripted mock transport simulating Kalshi GET responses."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def get(self, url: str, params: Mapping[str, str], timeout_seconds: float) -> HTTPResponse:
        self.calls.append((url, params, timeout_seconds))
        if not self.responses:
            raise KalshiTransportError(f"Unexpected GET {url}: no mock responses remaining")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def resp(
    payload: Any,
    status: int = 200,
    headers: Optional[Mapping[str, str]] = None,
) -> HTTPResponse:
    h = dict(headers) if headers is not None else {"Date": format_http_date()}
    return HTTPResponse(status_code=status, body=json.dumps(payload).encode("utf-8"), headers=h)


def market_payload(
    ticker: str,
    yes_ask: str = "0.40",
    no_ask: str = "0.55",
    updated_time: str = "2026-10-09T12:00:00Z",
) -> dict:
    return {
        "market": {
            "ticker": ticker,
            "event_ticker": "EV-TEST",
            "market_type": "binary",
            "status": "open",
            "title": f"Market {ticker}",
            "updated_time": updated_time,
            "yes_ask_dollars": yes_ask,
            "no_ask_dollars": no_ask,
            "yes_bid_dollars": "0.45",
            "no_bid_dollars": "0.60",
        }
    }


def book_payload(yes_bids=None, no_bids=None) -> dict:
    # YES ask 40¢ derived from NO bid 60¢
    # NO ask 55¢ derived from YES bid 45¢
    yb = yes_bids if yes_bids is not None else [["0.4500", "10.00"]]
    nb = no_bids if no_bids is not None else [["0.6000", "10.00"]]
    return {
        "orderbook_fp": {
            "yes_dollars": yb,
            "no_dollars": nb,
        }
    }


def test_complete_end_to_end_observation_to_replay_pipeline(tmp_path: Path):
    """Test full integration: Poll -> Detect -> Price -> Persist -> Paper Trade -> Reconcile -> Replay."""
    now_iso = datetime.now(timezone.utc).isoformat()

    # Cycle 1: Profitable market KX-ARB (YES ask 40¢, NO ask 55¢ -> Cost 95¢, Payout 100¢)
    # Market GET + OrderBook GET
    transport = MockTransport([
        resp(market_payload("KX-ARB", yes_ask="0.40", no_ask="0.55", updated_time=now_iso)),
        resp(book_payload()),
    ])

    client = KalshiRestClient(transport=transport)
    store = EvidenceStore(tmp_path / "evidence")
    portfolio = PaperPortfolio(initial_cash_cents=100_000)
    risk_manager = RiskManager(RiskConfig())

    config = MarketObserverConfig(
        poll_interval_seconds=0.0,
        max_stale_seconds=300.0,
        default_fee_per_contract_cents=1,
        enable_paper_trading=True,
        enforce_net_profitability=True,
        max_cycles=1,
    )

    observer = MarketObserver(
        client=client,
        config=config,
        target_tickers=["KX-ARB"],
        evidence_store=store,
        paper_portfolio=portfolio,
        risk_manager=risk_manager,
    )

    # 1. Run observer cycle
    observations, evaluations = observer.run_cycle()

    assert len(observations) == 1
    assert observations[0].is_success is True
    assert observations[0].is_stale is False

    assert len(evaluations) == 1
    eval_opp = evaluations[0]
    assert eval_opp.is_qualified is True
    assert eval_opp.status == STATUS_QUALIFIED
    assert eval_opp.paper_trade_id is not None
    assert eval_opp.pricing_result.gross_profit_cents == 5
    assert eval_opp.pricing_result.net_profit_cents == 3

    # 2. Verify V4 portfolio accounting & reconciliation
    rec = portfolio.reconcile()
    assert rec.is_reconciled is True
    assert len(portfolio.positions) == 2  # (KX-ARB, YES) and (KX-ARB, NO)
    assert portfolio.total_fees_paid_cents == 2
    # Cash spent = 95¢ cost + 2¢ fees = 97¢
    assert portfolio.available_cash_cents == 100_000 - 97
    assert portfolio.book_value_equity_cents() == 100_000 - 2  # Book equity: cash + 95¢ cost basis

    # 3. Verify evidence stored on disk
    saved_obs = store.load_observations()
    saved_opps = store.load_opportunities()
    assert len(saved_obs) == 1
    assert len(saved_opps) == 1
    assert saved_opps[0].opportunity_id == "BP:KX-ARB"

    # 4. Verify offline replay without network calls
    replay_report = replay_recorded_evidence(
        store,
        initial_cash_cents=100_000,
        default_fee_per_contract_cents=1,
    )
    assert replay_report.metrics.opportunities_evaluated == 1
    assert replay_report.metrics.trades_accepted == 1
    assert replay_report.metrics.trades_filled == 1
    assert replay_report.metrics.total_cost_cents == 95
    assert replay_report.portfolio.reconcile().is_reconciled is True

    # 5. Verify metrics report formatting
    report = observer.get_report()
    summary = report.summary()
    assert "KALSHI ARBITRAGE ENGINE" in summary
    assert "Successfully Refreshed:            1" in summary
    assert "Qualified (Executable Depth & Edge):1" in summary
    assert "Trades Fully Filled:               1" in summary


def test_paper_trading_disabled_by_default(tmp_path: Path):
    now_iso = datetime.now(timezone.utc).isoformat()
    transport = MockTransport([
        resp(market_payload("KX-ARB", updated_time=now_iso)),
        resp(book_payload()),
    ])
    client = KalshiRestClient(transport=transport)
    portfolio = PaperPortfolio(initial_cash_cents=100_000)

    # Config with enable_paper_trading=False (default)
    config = MarketObserverConfig(
        poll_interval_seconds=0.0,
        enable_paper_trading=False,
    )
    observer = MarketObserver(
        client=client,
        config=config,
        target_tickers=["KX-ARB"],
        paper_portfolio=portfolio,
    )

    observations, evaluations = observer.run_cycle()
    assert len(evaluations) == 1
    assert evaluations[0].paper_trade_id is None
    # Portfolio must have zero mutations
    assert portfolio.available_cash_cents == 100_000
    assert len(portfolio.positions) == 0
    assert len(portfolio.ledger) == 1  # only initial DEPOSIT


def test_polling_deduplication_prevents_duplicate_paper_trades():
    now_iso = datetime.now(timezone.utc).isoformat()

    # 2 consecutive cycles observing the exact same opportunity
    transport = MockTransport([
        # Cycle 1
        resp(market_payload("KX-DEDUP", updated_time=now_iso)),
        resp(book_payload()),
        # Cycle 2
        resp(market_payload("KX-DEDUP", updated_time=now_iso)),
        resp(book_payload()),
    ])
    client = KalshiRestClient(transport=transport)
    portfolio = PaperPortfolio(initial_cash_cents=100_000)

    config = MarketObserverConfig(
        poll_interval_seconds=0.0,
        default_fee_per_contract_cents=1,
        enable_paper_trading=True,
    )
    observer = MarketObserver(
        client=client,
        config=config,
        target_tickers=["KX-DEDUP"],
        paper_portfolio=portfolio,
    )

    # Cycle 1
    obs1, evals1 = observer.run_cycle()
    assert len(evals1) == 1
    assert evals1[0].paper_trade_id is not None
    assert evals1[0].is_recurrent is False
    assert len(observer._paper_trades_history) == 1

    # Cycle 2
    obs2, evals2 = observer.run_cycle()
    assert len(evals2) == 1
    assert evals2[0].is_recurrent is True
    # Deduplication MUST prevent duplicate trade execution in cycle 2
    assert evals2[0].paper_trade_id is None
    assert len(observer._paper_trades_history) == 1

    # Verify positions and cash were not doubled
    assert portfolio.reconcile().is_reconciled is True
    assert portfolio.positions[("KX-DEDUP", YES)].quantity == 1


def test_reappearance_after_disappearance_allows_new_trade():
    now_iso = datetime.now(timezone.utc).isoformat()

    transport = MockTransport([
        # Cycle 1: Present (Arbitrage available)
        resp(market_payload("KX-REAPP", yes_ask="0.40", no_ask="0.55", updated_time=now_iso)),
        resp(book_payload()),
        # Cycle 2: Disappears (No edge, prices rise to 60¢ + 60¢ = 120¢)
        resp(market_payload("KX-REAPP", yes_ask="0.60", no_ask="0.60", updated_time=now_iso)),
        resp(book_payload(yes_bids=[["0.4000", "10.00"]], no_bids=[["0.4000", "10.00"]])),
        # Cycle 3: Reappears (Arbitrage available again)
        resp(market_payload("KX-REAPP", yes_ask="0.40", no_ask="0.55", updated_time=now_iso)),
        resp(book_payload()),
    ])
    client = KalshiRestClient(transport=transport)
    portfolio = PaperPortfolio(initial_cash_cents=100_000)

    config = MarketObserverConfig(
        poll_interval_seconds=0.0,
        default_fee_per_contract_cents=1,
        enable_paper_trading=True,
    )
    observer = MarketObserver(
        client=client,
        config=config,
        target_tickers=["KX-REAPP"],
        paper_portfolio=portfolio,
    )

    # Cycle 1: Appears -> trade 1 executed
    _, evals1 = observer.run_cycle()
    assert len(evals1) == 1
    assert evals1[0].paper_trade_id is not None
    assert len(observer._paper_trades_history) == 1

    # Cycle 2: Disappears -> no trade
    _, evals2 = observer.run_cycle()
    assert len(evals2) == 0

    # Cycle 3: Reappears -> legitimate new trade executed
    _, evals3 = observer.run_cycle()
    assert len(evals3) == 1
    assert evals3[0].paper_trade_id is not None
    assert len(observer._paper_trades_history) == 2

    assert portfolio.reconcile().is_reconciled is True
    assert portfolio.positions[("KX-REAPP", YES)].quantity == 2


def test_pre_trade_risk_limit_enforced_in_observer():
    now_iso = datetime.now(timezone.utc).isoformat()
    transport = MockTransport([
        resp(market_payload("KX-RISK", yes_ask="0.40", no_ask="0.55", updated_time=now_iso)),
        resp(book_payload()),
    ])
    client = KalshiRestClient(transport=transport)
    portfolio = PaperPortfolio(initial_cash_cents=100_000)

    # Risk limit: max cost per trade is 50¢ (trade cost is 95¢ -> exceeds limit)
    risk_config = RiskConfig(max_cost_per_trade_cents=50)
    risk_manager = RiskManager(risk_config)

    config = MarketObserverConfig(
        poll_interval_seconds=0.0,
        default_fee_per_contract_cents=1,
        enable_paper_trading=True,
    )
    observer = MarketObserver(
        client=client,
        config=config,
        target_tickers=["KX-RISK"],
        paper_portfolio=portfolio,
        risk_manager=risk_manager,
    )

    _, evals = observer.run_cycle()
    assert len(evals) == 1
    # Trade was rejected by pre-trade risk
    assert len(observer._paper_trades_history) == 1
    rejected_trade = observer._paper_trades_history[0]
    assert rejected_trade.state == TradeState.REJECTED
    assert "Risk limit exceeded" in rejected_trade.rejection_reason

    # Portfolio cash untouched
    assert portfolio.available_cash_cents == 100_000
    assert len(portfolio.positions) == 0


def test_intermittent_network_failure_recovery():
    now_iso = datetime.now(timezone.utc).isoformat()
    transport = MockTransport([
        # Cycle 1: HTTP 500 error on market fetch
        KalshiHTTPError(500, "/markets/KX-NET", "Internal Server Error"),
        # Cycle 2: Recovered and successful
        resp(market_payload("KX-NET", updated_time=now_iso)),
        resp(book_payload()),
    ])
    client = KalshiRestClient(transport=transport)

    config = MarketObserverConfig(poll_interval_seconds=0.0)
    observer = MarketObserver(
        client=client,
        config=config,
        target_tickers=["KX-NET"],
    )

    # Cycle 1: handles failure gracefully
    obs1, evals1 = observer.run_cycle()
    assert len(obs1) == 1
    assert obs1[0].is_success is False
    assert len(evals1) == 0

    # Cycle 2: recovers normally
    obs2, evals2 = observer.run_cycle()
    assert len(obs2) == 1
    assert obs2[0].is_success is True
    assert len(evals2) == 1


def test_mece_basket_complete_pipeline_simulation_and_persistence(tmp_path: Path):
    """F-05 & F-03: End-to-end integration for qualified MECE basket with paper trading and persistence."""
    now_iso = datetime.now(timezone.utc).isoformat()
    transport = MockTransport([
        # Leg A: KX-A (YES ask 40¢ via NO bid 60¢)
        resp(market_payload("KX-A", yes_ask="0.40", no_ask="0.60", updated_time=now_iso)),
        resp(book_payload(yes_bids=[], no_bids=[["0.6000", "10.00"]])),
        # Leg B: KX-B (YES ask 55¢ via NO bid 45¢)
        resp(market_payload("KX-B", yes_ask="0.55", no_ask="0.45", updated_time=now_iso)),
        resp(book_payload(yes_bids=[], no_bids=[["0.4500", "10.00"]])),
    ])
    client = KalshiRestClient(transport=transport)
    store = EvidenceStore(tmp_path / "evidence")
    portfolio = PaperPortfolio(initial_cash_cents=100_000)
    risk_manager = RiskManager(RiskConfig())

    basket = DeclaredMeceBasket(
        event_ticker="EV-MECE-E2E",
        outcomes=("CAND_A", "CAND_B"),
        # Inverted mapping dictionary order to verify F-03 end-to-end
        market_outcome_map={"KX-B": "CAND_B", "KX-A": "CAND_A"},
        basket_side=YES,
    )
    config = MarketObserverConfig(
        poll_interval_seconds=0.0,
        max_stale_seconds=300.0,
        default_fee_per_contract_cents=1,
        enable_paper_trading=True,
        enforce_net_profitability=True,
        max_cycles=1,
    )
    observer = MarketObserver(
        client=client,
        config=config,
        declared_baskets=[basket],
        evidence_store=store,
        paper_portfolio=portfolio,
        risk_manager=risk_manager,
    )

    observations, evaluations = observer.run_cycle()

    assert len(observations) == 2
    assert len(evaluations) == 1
    opp = evaluations[0]
    assert opp.is_qualified is True
    assert opp.status == STATUS_QUALIFIED
    assert opp.paper_trade_id is not None
    assert opp.market_tickers == ("KX-A", "KX-B")
    assert opp.pricing_result.gross_profit_cents == 5
    assert opp.pricing_result.net_profit_cents == 3

    # Verify portfolio state & accounting
    assert portfolio.reconcile().is_reconciled is True
    assert portfolio.available_cash_cents == 100_000 - 97  # 95¢ cost + 2¢ fees
    assert portfolio.positions[("KX-A", YES)].total_cost_cents == 40
    assert portfolio.positions[("KX-B", YES)].total_cost_cents == 55

    # Verify evidence persistence
    saved_opps = store.load_opportunities()
    assert len(saved_opps) == 1
    assert saved_opps[0].opportunity_id == "MECE:EV-MECE-E2E:YES"
    assert saved_opps[0].is_qualified is True
    assert saved_opps[0].market_tickers == ("KX-A", "KX-B")

