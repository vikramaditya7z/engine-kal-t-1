"""Unit tests for V5 MarketObserver: discovery, polling, freshness, skew, and candidate detection."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any, Mapping, Optional

import pytest

from kalshi_arbitrage import (
    DeclaredMeceBasket,
    EvidenceStore,
    HTTPResponse,
    KalshiHTTPError,
    KalshiRestClient,
    KalshiTransportError,
    MarketObservation,
    MarketObserver,
    MarketObserverConfig,
    NormalizedOrderBook,
    OrderBookLevel,
    PaperPortfolio,
    STATUS_QUALIFIED,
    STATUS_REJECTED_INSUFFICIENT_DEPTH,
    STATUS_REJECTED_LEG_SKEW,
    STATUS_REJECTED_MISSING_TIMESTAMP,
    STATUS_REJECTED_NO_GROSS_EDGE,
    STATUS_REJECTED_STALE,
    STATUS_REJECTED_UNPROFITABLE_FEES,
    YES,
    format_http_date,
    parse_http_date,
)


class FakeTransport:
    """Mock HTTP transport for testing without live network calls."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def get(self, url: str, params: Mapping[str, str], timeout_seconds: float) -> HTTPResponse:
        self.calls.append((url, params, timeout_seconds))
        if not self.responses:
            raise KalshiTransportError(f"No mock response queued for GET {url}")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def json_response(
    payload: Any,
    status: int = 200,
    headers: Optional[Mapping[str, str]] = None,
) -> HTTPResponse:
    h = dict(headers) if headers is not None else {"Date": format_http_date()}
    return HTTPResponse(status_code=status, body=json.dumps(payload).encode("utf-8"), headers=h)


def make_market_payload(
    ticker: str,
    event_ticker: str = "EVENT-1",
    yes_ask: str = "0.40",
    no_ask: str = "0.55",
    updated_time: str = "2026-10-09T12:00:00Z",
) -> dict:
    return {
        "market": {
            "ticker": ticker,
            "event_ticker": event_ticker,
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


def make_orderbook_payload(
    yes_bids=None,
    no_bids=None,
) -> dict:
    # Complementary asks in Kalshi binary markets:
    # To buy YES at 40¢, resting NO bid is at 60¢ (100 - 40 = 60).
    # To buy NO at 55¢, resting YES bid is at 45¢ (100 - 55 = 45).
    yb = yes_bids if yes_bids is not None else [["0.4500", "10.00"]]
    nb = no_bids if no_bids is not None else [["0.6000", "10.00"]]
    return {
        "orderbook_fp": {
            "yes_dollars": yb,
            "no_dollars": nb,
        }
    }


def test_market_discovery_pagination():
    fake = FakeTransport([
        json_response({
            "markets": [
                {
                    "ticker": "KX-1",
                    "event_ticker": "EV-1",
                    "market_type": "binary",
                    "status": "open",
                    "title": "M1",
                }
            ],
            "cursor": "cur-1",
        }),
        json_response({
            "markets": [
                {
                    "ticker": "KX-2",
                    "event_ticker": "EV-1",
                    "market_type": "binary",
                    "status": "open",
                    "title": "M2",
                }
            ],
            "cursor": "",
        }),
    ])
    client = KalshiRestClient(transport=fake)
    observer = MarketObserver(client=client)

    discovered = observer.discover_markets()
    assert discovered == ("KX-1", "KX-2")
    assert len(fake.calls) == 2


def test_successful_market_polling():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = FakeTransport([
        json_response(make_market_payload("KX-TEST", updated_time=now_iso)),
        json_response(make_orderbook_payload()),
    ])
    client = KalshiRestClient(transport=fake)
    observer = MarketObserver(client=client, config=MarketObserverConfig(max_stale_seconds=300.0))

    obs = observer.poll_market("KX-TEST")
    assert obs.is_success is True
    assert obs.is_stale is False
    assert obs.ticker == "KX-TEST"
    assert obs.order_book is not None
    assert len(obs.order_book.yes_bids) == 1
    assert obs.market is not None
    assert obs.source_timestamp is not None


def test_failed_refresh_does_not_reuse_stale_data():
    fake = FakeTransport([
        KalshiHTTPError(500, "/markets/KX-TEST", "Internal Server Error"),
    ])
    client = KalshiRestClient(transport=fake)
    observer = MarketObserver(client=client)

    obs = observer.poll_market("KX-TEST")
    assert obs.is_success is False
    assert obs.is_stale is True
    assert obs.order_book is None
    assert obs.market is None
    assert "500" in obs.error_message


def test_stale_order_book_rejection():
    # Source timestamp from 2 hours ago
    stale_dt = datetime.now(timezone.utc) - timedelta(hours=2)
    fake = FakeTransport([
        json_response(make_market_payload("KX-OLD")),
        json_response(make_orderbook_payload(), headers={"Date": format_http_date(stale_dt)}),
    ])
    client = KalshiRestClient(transport=fake)
    observer = MarketObserver(
        client=client,
        config=MarketObserverConfig(max_stale_seconds=60.0),
    )

    obs = observer.poll_market("KX-OLD")
    assert obs.is_success is True
    assert obs.is_stale is True
    assert "stale" in obs.staleness_reason.lower()


def test_missing_source_timestamp_rejection():
    # Order book response without HTTP Date header
    fake = FakeTransport([
        json_response(make_market_payload("KX-NOTS")),
        json_response(make_orderbook_payload(), headers={}),
    ])
    client = KalshiRestClient(transport=fake)
    observer = MarketObserver(
        client=client,
        config=MarketObserverConfig(require_source_timestamp=True),
    )

    obs = observer.poll_market("KX-NOTS")
    assert obs.is_success is True
    assert obs.is_stale is True
    assert obs.staleness_reason == "Missing exchange source timestamp"


def test_orderbook_date_header_valid_propagation():
    fixed_now = datetime(2026, 10, 9, 12, 0, 5, tzinfo=timezone.utc)
    valid_dt = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    fake = FakeTransport([
        json_response(make_market_payload("KX-VALID")),
        json_response(make_orderbook_payload(), headers={"Date": format_http_date(valid_dt)}),
    ])
    client = KalshiRestClient(transport=fake)
    observer = MarketObserver(
        client=client,
        config=MarketObserverConfig(max_stale_seconds=300.0),
        time_provider=lambda: fixed_now,
    )

    obs = observer.poll_market("KX-VALID")
    assert obs.is_success is True
    assert obs.is_stale is False
    assert obs.source_timestamp == valid_dt
    assert obs.order_book is not None
    assert obs.order_book.source_timestamp == valid_dt


def test_orderbook_date_header_malformed_rejection():
    fake = FakeTransport([
        json_response(make_market_payload("KX-MALFORMED")),
        json_response(make_orderbook_payload(), headers={"Date": "Not-A-Valid-HTTP-Date"}),
    ])
    client = KalshiRestClient(transport=fake)
    observer = MarketObserver(client=client)

    obs = observer.poll_market("KX-MALFORMED")
    assert obs.is_success is False
    assert obs.is_stale is True
    assert obs.source_timestamp is None
    assert obs.staleness_reason == "Malformed exchange source timestamp"


def test_orderbook_date_header_future_rejection():
    fixed_now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    future_dt = fixed_now + timedelta(seconds=15)  # 15s in the future (> 10s allowed drift)
    fake = FakeTransport([
        json_response(make_market_payload("KX-FUT")),
        json_response(make_orderbook_payload(), headers={"Date": format_http_date(future_dt)}),
    ])
    client = KalshiRestClient(transport=fake)
    observer = MarketObserver(client=client, time_provider=lambda: fixed_now)

    obs = observer.poll_market("KX-FUT")
    assert obs.is_success is True
    assert obs.is_stale is True
    assert "future" in obs.staleness_reason.lower()


def test_orderbook_freshness_independent_of_market_updated_time():
    fixed_now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    ancient_iso = (fixed_now - timedelta(days=30)).isoformat()
    fresh_date = format_http_date(fixed_now - timedelta(seconds=2))

    # Case A: Market metadata updated 30 days ago, orderbook Date is fresh -> VALID
    fake_fresh = FakeTransport([
        json_response(make_market_payload("KX-DECOUPLED", updated_time=ancient_iso)),
        json_response(make_orderbook_payload(), headers={"Date": fresh_date}),
    ])
    client_fresh = KalshiRestClient(transport=fake_fresh)
    observer_fresh = MarketObserver(
        client=client_fresh,
        config=MarketObserverConfig(max_stale_seconds=60.0),
        time_provider=lambda: fixed_now,
    )
    obs_fresh = observer_fresh.poll_market("KX-DECOUPLED")
    assert obs_fresh.is_success is True
    assert obs_fresh.is_stale is False
    assert obs_fresh.source_timestamp == parse_http_date(fresh_date)

    # Case B: Market metadata updated just now, orderbook Date is 2 hours old -> STALE
    recent_iso = fixed_now.isoformat()
    old_date = format_http_date(fixed_now - timedelta(hours=2))

    fake_old = FakeTransport([
        json_response(make_market_payload("KX-DECOUPLED", updated_time=recent_iso)),
        json_response(make_orderbook_payload(), headers={"Date": old_date}),
    ])
    client_old = KalshiRestClient(transport=fake_old)
    observer_old = MarketObserver(
        client=client_old,
        config=MarketObserverConfig(max_stale_seconds=60.0),
        time_provider=lambda: fixed_now,
    )
    obs_old = observer_old.poll_market("KX-DECOUPLED")
    assert obs_old.is_success is True
    assert obs_old.is_stale is True
    assert "stale" in obs_old.staleness_reason.lower()


def test_binary_parity_candidate_profitable_qualification():
    now_iso = datetime.now(timezone.utc).isoformat()
    # YES ask = 40¢, NO ask = 55¢ -> total cost 95¢, payout 100¢ -> 5¢ gross edge
    # Fee = 1¢ * 2 = 2¢ -> 3¢ net edge
    fake = FakeTransport([
        json_response(make_market_payload("KX-ARB", yes_ask="0.40", no_ask="0.55", updated_time=now_iso)),
        json_response(make_orderbook_payload(
            yes_bids=[["0.4500", "10.00"]],  # NO ask = 55¢ (qty 10)
            no_bids=[["0.6000", "10.00"]],   # YES ask = 40¢ (qty 10)
        )),
    ])
    client = KalshiRestClient(transport=fake)
    config = MarketObserverConfig(
        max_stale_seconds=300.0,
        default_fee_per_contract_cents=1,
        enforce_net_profitability=True,
    )
    observer = MarketObserver(client=client, config=config)

    obs = observer.poll_market("KX-ARB")
    opp = observer.evaluate_binary_parity_candidate(obs)

    assert opp is not None
    assert opp.is_qualified is True
    assert opp.status == STATUS_QUALIFIED
    assert opp.pricing_result is not None
    assert opp.pricing_result.gross_profit_cents == 5
    assert opp.pricing_result.net_profit_cents == 3


def test_binary_parity_candidate_rejected_after_fees():
    now_iso = datetime.now(timezone.utc).isoformat()
    # YES ask = 49¢, NO ask = 50¢ -> total cost 99¢, payout 100¢ -> 1¢ gross edge
    # Fee = 1¢ * 2 = 2¢ -> net edge -1¢
    fake = FakeTransport([
        json_response(make_market_payload("KX-FEE", yes_ask="0.49", no_ask="0.50", updated_time=now_iso)),
        json_response(make_orderbook_payload(
            yes_bids=[["0.5000", "10.00"]],  # NO ask = 50¢
            no_bids=[["0.5100", "10.00"]],   # YES ask = 49¢
        )),
    ])
    client = KalshiRestClient(transport=fake)
    config = MarketObserverConfig(
        max_stale_seconds=300.0,
        default_fee_per_contract_cents=1,
        enforce_net_profitability=True,
    )
    observer = MarketObserver(client=client, config=config)

    obs = observer.poll_market("KX-FEE")
    opp = observer.evaluate_binary_parity_candidate(obs)

    assert opp is not None
    assert opp.is_qualified is False
    assert opp.status == STATUS_REJECTED_UNPROFITABLE_FEES


def test_binary_parity_insufficient_depth_rejection():
    now_iso = datetime.now(timezone.utc).isoformat()
    # Empty book
    fake = FakeTransport([
        json_response(make_market_payload("KX-NODEPTH", yes_ask="0.40", no_ask="0.55", updated_time=now_iso)),
        json_response(make_orderbook_payload(yes_bids=[], no_bids=[])),
    ])
    client = KalshiRestClient(transport=fake)
    observer = MarketObserver(client=client, config=MarketObserverConfig(max_stale_seconds=300.0))

    obs = observer.poll_market("KX-NODEPTH")
    opp = observer.evaluate_binary_parity_candidate(obs)

    assert opp is not None
    assert opp.is_qualified is False
    assert opp.status == STATUS_REJECTED_INSUFFICIENT_DEPTH


def test_multi_leg_timestamp_skew_rejection():
    now = datetime.now(timezone.utc)
    t1 = now
    t2 = now - timedelta(seconds=20)  # 20s skew

    fake = FakeTransport([
        json_response(make_market_payload("KX-A", yes_ask="0.30", no_ask="0.70")),
        json_response(make_orderbook_payload(), headers={"Date": format_http_date(t1)}),
        json_response(make_market_payload("KX-B", yes_ask="0.60", no_ask="0.40")),
        json_response(make_orderbook_payload(), headers={"Date": format_http_date(t2)}),
    ])
    client = KalshiRestClient(transport=fake)
    basket = DeclaredMeceBasket(
        event_ticker="EV-PRES",
        outcomes=("CAND_A", "CAND_B"),
        market_outcome_map={"KX-A": "CAND_A", "KX-B": "CAND_B"},
        basket_side=YES,
    )
    config = MarketObserverConfig(
        max_stale_seconds=300.0,
        max_leg_timestamp_skew_seconds=5.0,  # Max 5s skew allowed
    )
    observer = MarketObserver(client=client, config=config, declared_baskets=[basket])

    obs_a = observer.poll_market("KX-A")
    obs_b = observer.poll_market("KX-B")

    opp = observer.evaluate_mece_basket_candidate(basket, {"KX-A": obs_a, "KX-B": obs_b})

    assert opp is not None
    assert opp.is_qualified is False
    assert opp.status == STATUS_REJECTED_LEG_SKEW
    assert opp.leg_timestamp_skew_seconds >= 19.0


def test_observer_clean_shutdown_and_bounded_cycles():
    now_iso = datetime.now(timezone.utc).isoformat()
    fake = FakeTransport([
        json_response(make_market_payload("KX-1", updated_time=now_iso)),
        json_response(make_orderbook_payload()),
        json_response(make_market_payload("KX-1", updated_time=now_iso)),
        json_response(make_orderbook_payload()),
    ])
    client = KalshiRestClient(transport=fake)
    config = MarketObserverConfig(
        poll_interval_seconds=0.0,
        max_cycles=2,
    )
    observer = MarketObserver(
        client=client,
        config=config,
        target_tickers=["KX-1"],
    )

    observer.run()
    assert observer._cycle_count == 2
    assert len(fake.calls) == 4


def test_discover_markets_respects_max_discovered_markets():
    fake = FakeTransport([
        json_response({
            "markets": [
                {"ticker": f"KX-{i}", "event_ticker": "EV-1", "market_type": "binary", "status": "open", "title": f"M{i}"}
                for i in range(10)
            ],
            "cursor": "next",
        }),
    ])
    client = KalshiRestClient(transport=fake)
    # Configure discovery limit of 3
    config = MarketObserverConfig(max_discovered_markets=3)
    observer = MarketObserver(client=client, config=config)

    discovered = observer.discover_markets()
    assert len(discovered) == 3
    assert discovered == ("KX-0", "KX-1", "KX-2")


def test_discover_markets_is_cached_across_cycles():
    fake = FakeTransport([
        # Discovery call
        json_response({
            "markets": [
                {"ticker": "KX-A", "event_ticker": "EV-1", "market_type": "binary", "status": "open", "title": "A"},
            ],
            "cursor": "",
        }),
        # Cycle 1 market + order book
        json_response(make_market_payload("KX-A")),
        json_response(make_orderbook_payload()),
        # Cycle 2 market + order book (MUST NOT re-fetch /markets discovery!)
        json_response(make_market_payload("KX-A")),
        json_response(make_orderbook_payload()),
    ])
    client = KalshiRestClient(transport=fake)
    config = MarketObserverConfig(poll_interval_seconds=0.0, max_cycles=2)
    observer = MarketObserver(client=client, config=config)

    observer.run()
    assert observer._cycle_count == 2
    # Exactly 5 calls: 1 discovery + 2 calls in cycle 1 + 2 calls in cycle 2
    assert len(fake.calls) == 5
    assert fake.calls[0][0].endswith("/markets")
    assert not fake.calls[3][0].endswith("/markets")


def test_max_poll_markets_per_cycle_bounds_polling_loop():
    now_iso = datetime.now(timezone.utc).isoformat()
    # 5 targets provided, but max_poll_markets_per_cycle = 2
    fake = FakeTransport([
        json_response(make_market_payload("KX-1", updated_time=now_iso)),
        json_response(make_orderbook_payload()),
        json_response(make_market_payload("KX-2", updated_time=now_iso)),
        json_response(make_orderbook_payload()),
    ])
    client = KalshiRestClient(transport=fake)
    config = MarketObserverConfig(
        poll_interval_seconds=0.0,
        max_poll_markets_per_cycle=2,
        max_cycles=1,
    )
    observer = MarketObserver(
        client=client,
        config=config,
        target_tickers=["KX-1", "KX-2", "KX-3", "KX-4", "KX-5"],
    )

    obs, _ = observer.run_cycle()
    assert len(obs) == 2
    assert len(fake.calls) == 4


def test_progress_callback_receives_expected_updates():
    now_iso = datetime.now(timezone.utc).isoformat()
    progress_messages = []

    fake = FakeTransport([
        json_response(make_market_payload("KX-PROG", updated_time=now_iso)),
        json_response(make_orderbook_payload()),
    ])
    client = KalshiRestClient(transport=fake)
    config = MarketObserverConfig(poll_interval_seconds=0.0, max_cycles=1)
    observer = MarketObserver(
        client=client,
        config=config,
        target_tickers=["KX-PROG"],
        progress_callback=progress_messages.append,
    )

    observer.run()
    assert len(progress_messages) >= 2
    assert any("Polling 1 markets" in m for m in progress_messages)
    assert any("Complete" in m for m in progress_messages)


def test_observer_interrupt_during_sleep_exits_promptly():
    import time

    client = KalshiRestClient(transport=FakeTransport())
    config = MarketObserverConfig(poll_interval_seconds=10.0)
    observer = MarketObserver(client=client, config=config)

    # Calling stop() then sleep should exit immediately (< 0.5s) instead of 10s
    t0 = time.monotonic()
    observer.stop()
    observer._sleep_with_stop_check(10.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5


def test_observer_keyboard_interrupt_handled_gracefully():
    class InterruptingTransport:
        def get(self, url, params, timeout):
            raise KeyboardInterrupt()

    client = KalshiRestClient(transport=InterruptingTransport())
    config = MarketObserverConfig(poll_interval_seconds=0.0, max_cycles=2)
    observer = MarketObserver(client=client, config=config, target_tickers=["KX-INT"])

    # run() must catch KeyboardInterrupt cleanly without crashing
    observer.run()
    assert observer._stop_requested is True


def test_mece_basket_leg_ticker_alignment_with_inverted_mapping_order():
    """F-03: Ensure MECE outcomes, execution traversals, and paper fills are strictly aligned.

    Tests that deliberately inverting dictionary insertion order in market_outcome_map
    does not transpose legs, fills, or position accounting.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    # KX-A: YES ask = 40¢ (resting NO bid 60¢)
    # KX-B: YES ask = 55¢ (resting NO bid 45¢)
    # Deliberately inverted dictionary insertion order: KX-B first, then KX-A
    mapping = {"KX-B": "CAND_B", "KX-A": "CAND_A"}
    basket = DeclaredMeceBasket(
        event_ticker="EV-PRES",
        outcomes=("CAND_A", "CAND_B"),
        market_outcome_map=mapping,
        basket_side=YES,
    )
    # Ensure ordered_tickers strictly matches declared outcomes order
    assert basket.ordered_tickers == ("KX-A", "KX-B")

    fake = FakeTransport([
        json_response(make_market_payload("KX-A", yes_ask="0.40", no_ask="0.60", updated_time=now_iso)),
        json_response(make_orderbook_payload(yes_bids=[], no_bids=[["0.6000", "10.00"]])),
        json_response(make_market_payload("KX-B", yes_ask="0.55", no_ask="0.45", updated_time=now_iso)),
        json_response(make_orderbook_payload(yes_bids=[], no_bids=[["0.4500", "10.00"]])),
    ])
    client = KalshiRestClient(transport=fake)
    portfolio = PaperPortfolio(initial_cash_cents=100_000)
    config = MarketObserverConfig(
        poll_interval_seconds=0.0,
        max_cycles=1,
        default_fee_per_contract_cents=1,
        enable_paper_trading=True,
    )
    observer = MarketObserver(
        client=client,
        config=config,
        declared_baskets=[basket],
        paper_portfolio=portfolio,
    )

    obs, evals = observer.run_cycle()
    assert len(evals) == 1
    opp = evals[0]
    assert opp.is_qualified is True
    # opp.market_tickers must match basket.outcomes order ("KX-A", "KX-B")
    assert opp.market_tickers == ("KX-A", "KX-B")

    # Paper trade leg alignment
    assert len(observer._paper_trades_history) == 1
    trade = observer._paper_trades_history[0]
    assert len(trade.legs) == 2

    # Leg 0 must be CAND_A (KX-A at 40¢)
    assert trade.legs[0].contract_id == "KX-A"
    assert trade.legs[0].market_ticker == "KX-A"
    assert trade.legs[0].total_cost_cents == 40

    # Leg 1 must be CAND_B (KX-B at 55¢)
    assert trade.legs[1].contract_id == "KX-B"
    assert trade.legs[1].market_ticker == "KX-B"
    assert trade.legs[1].total_cost_cents == 55

    # Portfolio positions must NOT be transposed
    assert portfolio.positions[("KX-A", YES)].quantity == 1
    assert portfolio.positions[("KX-A", YES)].total_cost_cents == 40
    assert portfolio.positions[("KX-B", YES)].quantity == 1
    assert portfolio.positions[("KX-B", YES)].total_cost_cents == 55


def test_opportunity_lifetime_tracking_across_reappearance_episodes():
    """F-02: Lifetime must measure continuous episodes, not spanning inactive gaps."""
    t1 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 9, 12, 0, 10, tzinfo=timezone.utc)
    # Episode 1 disappears at t3
    # Episode 2 appears at t4, persists at t5
    t4 = datetime(2026, 10, 9, 12, 5, 0, tzinfo=timezone.utc)
    t5 = datetime(2026, 10, 9, 12, 5, 15, tzinfo=timezone.utc)

    # Cycle 1: present at t1
    # Cycle 2: present at t2
    # Cycle 3: absent
    # Cycle 4: reappears at t4
    # Cycle 5: persists at t5
    # Cycle 6: absent again
    fake = FakeTransport([
        # C1
        json_response(make_market_payload("KX-ARB", yes_ask="0.40", no_ask="0.55", updated_time=t1.isoformat())),
        json_response(make_orderbook_payload()),
        # C2
        json_response(make_market_payload("KX-ARB", yes_ask="0.40", no_ask="0.55", updated_time=t2.isoformat())),
        json_response(make_orderbook_payload()),
        # C3
        json_response(make_market_payload("KX-ARB", yes_ask="0.60", no_ask="0.60", updated_time=t2.isoformat())),
        json_response(make_orderbook_payload(yes_bids=[["0.4000", "10.00"]], no_bids=[["0.4000", "10.00"]])),
        # C4
        json_response(make_market_payload("KX-ARB", yes_ask="0.40", no_ask="0.55", updated_time=t4.isoformat())),
        json_response(make_orderbook_payload()),
        # C5
        json_response(make_market_payload("KX-ARB", yes_ask="0.40", no_ask="0.55", updated_time=t5.isoformat())),
        json_response(make_orderbook_payload()),
        # C6
        json_response(make_market_payload("KX-ARB", yes_ask="0.60", no_ask="0.60", updated_time=t5.isoformat())),
        json_response(make_orderbook_payload(yes_bids=[["0.4000", "10.00"]], no_bids=[["0.4000", "10.00"]])),
    ])
    client = KalshiRestClient(transport=fake)
    config = MarketObserverConfig(
        poll_interval_seconds=0.0,
        max_cycles=6,
        max_stale_seconds=600.0,
    )
    cycle_timestamps = {
        1: t1,
        2: t2,
        3: t2 + timedelta(seconds=1),
        4: t4,
        5: t5,
        6: t5 + timedelta(seconds=1),
    }
    observer = MarketObserver(
        client=client,
        config=config,
        target_tickers=["KX-ARB"],
        time_provider=lambda: cycle_timestamps.get(observer._cycle_count, t1),
    )
    observer.run()

    # Must record exactly 2 episode lifetimes:
    # Episode 1: t2 - t1 = 10.0 seconds
    # Episode 2: t5 - t4 = 15.0 seconds
    assert len(observer._opportunity_lifetimes) == 2
    assert observer._opportunity_lifetimes[0] == 10.0
    assert observer._opportunity_lifetimes[1] == 15.0


def test_discovery_cache_refreshes_at_configured_interval():
    """F-01: Discovery cache is reused between cycles and refreshed at configured cycle interval."""
    fake = FakeTransport([
        # Cycle 1 discovery: returns KX-1
        json_response({"markets": [{"ticker": "KX-1", "event_ticker": "EV-1", "market_type": "binary", "status": "open", "title": "M1"}], "cursor": ""}),
        json_response(make_market_payload("KX-1")),
        json_response(make_orderbook_payload()),
        # Cycle 2: KX-1 (cached discovery, no discovery GET)
        json_response(make_market_payload("KX-1")),
        json_response(make_orderbook_payload()),
        # Cycle 3: discovery refresh triggered! Returns KX-2
        json_response({"markets": [{"ticker": "KX-2", "event_ticker": "EV-1", "market_type": "binary", "status": "open", "title": "M2"}], "cursor": ""}),
        json_response(make_market_payload("KX-2")),
        json_response(make_orderbook_payload()),
    ])
    client = KalshiRestClient(transport=fake)
    # Refresh discovery every 2 cycles
    config = MarketObserverConfig(
        poll_interval_seconds=0.0,
        max_cycles=3,
        discovery_refresh_interval_cycles=2,
    )
    observer = MarketObserver(client=client, config=config)
    observer.run()

    # Total calls:
    # Call 0: GET /markets (discovery C1)
    # Call 1: GET /markets/KX-1
    # Call 2: GET /markets/KX-1/orderbook
    # Call 3: GET /markets/KX-1
    # Call 4: GET /markets/KX-1/orderbook
    # Call 5: GET /markets (discovery C3 refresh!)
    # Call 6: GET /markets/KX-2
    # Call 7: GET /markets/KX-2/orderbook
    assert len(fake.calls) == 8
    assert fake.calls[0][0].endswith("/markets")
    assert not fake.calls[3][0].endswith("/markets")
    assert fake.calls[5][0].endswith("/markets")


def test_explicit_tickers_not_truncated_when_exceeding_default_limit():
    """F-04: Explicit tickers must not be silently truncated by default limits."""
    from scripts.run_observer import main as cli_main

    # 1. Programmatic default config honors all target tickers
    client = KalshiRestClient(transport=FakeTransport())
    target_list = [f"KX-{i}" for i in range(25)]
    observer = MarketObserver(client=client, target_tickers=target_list)
    assert observer.config.max_poll_markets_per_cycle >= 25

    # 2. CLI validation error when explicit tickers exceed --max-markets
    err_code = cli_main(["--tickers", "KX-1", "KX-2", "KX-3", "--max-markets", "2"])
    assert err_code == 2


def test_mece_basket_positive_qualification_and_evidence(tmp_path):
    """F-05: Positive observer-level integration test for a qualified MECE basket."""
    now_iso = datetime.now(timezone.utc).isoformat()
    # Team X ask = 35¢ (resting NO bid 65¢)
    # Team Y ask = 60¢ (resting NO bid 40¢)
    # Total cost = 95¢, Payout = 100¢ -> 5¢ gross edge
    # Fee = 1¢ * 2 = 2¢ -> 3¢ net edge
    fake = FakeTransport([
        json_response(make_market_payload("KX-X", yes_ask="0.35", no_ask="0.65", updated_time=now_iso)),
        json_response(make_orderbook_payload(yes_bids=[], no_bids=[["0.6500", "10.00"]])),
        json_response(make_market_payload("KX-Y", yes_ask="0.60", no_ask="0.40", updated_time=now_iso)),
        json_response(make_orderbook_payload(yes_bids=[], no_bids=[["0.4000", "10.00"]])),
    ])
    client = KalshiRestClient(transport=fake)
    store = EvidenceStore(tmp_path / "evidence")
    basket = DeclaredMeceBasket(
        event_ticker="EV-WIN",
        outcomes=("OUT_X", "OUT_Y"),
        market_outcome_map={"KX-X": "OUT_X", "KX-Y": "OUT_Y"},
        basket_side=YES,
    )
    config = MarketObserverConfig(
        poll_interval_seconds=0.0,
        max_cycles=1,
        default_fee_per_contract_cents=1,
        enforce_net_profitability=True,
    )
    observer = MarketObserver(
        client=client,
        config=config,
        declared_baskets=[basket],
        evidence_store=store,
    )

    obs, evals = observer.run_cycle()
    assert len(evals) == 1
    opp = evals[0]

    assert opp.is_qualified is True
    assert opp.status == STATUS_QUALIFIED
    assert opp.strategy_type == "mece_basket_long_yes"
    assert opp.market_tickers == ("KX-X", "KX-Y")
    assert opp.pricing_result is not None
    assert opp.pricing_result.gross_profit_cents == 5
    assert opp.pricing_result.net_profit_cents == 3

    # Check evidence persistence
    saved_opps = store.load_opportunities()
    assert len(saved_opps) == 1
    assert saved_opps[0].opportunity_id == "MECE:EV-WIN:YES"
    assert saved_opps[0].is_qualified is True
    assert saved_opps[0].market_tickers == ("KX-X", "KX-Y")


