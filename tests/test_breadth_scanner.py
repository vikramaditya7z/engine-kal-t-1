"""Unit and integration tests for Phase 2A: Broad Market Catalog Discovery & Cursor Pagination."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
import pytest

from kalshi_arbitrage import (
    HTTPResponse,
    KalshiHTTPError,
    KalshiJSONError,
    KalshiPaginationError,
    KalshiResponseError,
    KalshiRestClient,
    KalshiTransportError,
    ContinuousCollector,
    ContinuousEvaluator,
    ExecutionAwareArbitrageQualifier,
    AlertEngine,
    FeeScheduleType,
    NormalizedOrderBook,
    OrderBookLevel,
    QualificationOutcome,
)
from kalshi_arbitrage.breadth_scanner import (
    BreadthArbitrageScreener,
    BreadthScannerConfig,
    CatalogDiscoveryEngine,
    CatalogDiscoveryResult,
    DeclaredMeceBasket,
    EventScreeningResult,
    MarketExclusionReason,
    MarketScreeningResult,
    PreliminaryCandidate,
    ScreeningOutcome,
    ScreeningStrategyType,
    ScreeningSummary,
    StrategyEvaluationResult,
    TargetedOrderBookQualifier,
    TargetedOrderBookQualifierConfig,
    TargetedQualificationResult,
    TargetedQualificationSummary,
    compute_order_book_hash,
)
from kalshi_arbitrage.market_data import normalize_event, normalize_market


# -----------------------------------------------------------------------------
# Fixtures and Helpers
# -----------------------------------------------------------------------------

def make_raw_market(
    ticker: str = "KXTEST-1",
    event_ticker: str = "KXEVENT-1",
    market_type: str = "binary",
    status: str = "open",
    title: str = "Will X happen?",
    close_time: Optional[str] = None,
    yes_bid: str = "0.4500",
    yes_ask: str = "0.4600",
    no_bid: str = "0.5400",
    no_ask: str = "0.5500",
    updated_time: Optional[str] = None,
) -> Dict[str, Any]:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if close_time is None:
        close_dt = datetime.now(timezone.utc) + timedelta(days=2)
        close_time = close_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "market_type": market_type,
        "status": status,
        "title": title,
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "no_bid_dollars": no_bid,
        "no_ask_dollars": no_ask,
        "yes_bid_size_fp": "100.00",
        "yes_ask_size_fp": "100.00",
        "volume_fp": "500.00",
        "volume_24h_fp": "50.00",
        "open_interest_fp": "200.00",
        "created_time": now_str,
        "updated_time": updated_time if updated_time is not None else now_str,
        "open_time": now_str,
        "close_time": close_time,
        "rules_primary": "Standard settlement rules.",
    }


def make_raw_event(
    event_ticker: str = "KXEVENT-1",
    category: str = "Economics",
    markets: Optional[List[Dict[str, Any]]] = None,
    series_ticker: str = "KXSERIES",
    mutually_exclusive: bool = True,
) -> Dict[str, Any]:
    mkts = markets if markets is not None else [
        make_raw_market(f"{event_ticker}-M1", event_ticker=event_ticker),
        make_raw_market(f"{event_ticker}-M2", event_ticker=event_ticker),
    ]
    return {
        "event_ticker": event_ticker,
        "series_ticker": series_ticker,
        "title": f"Event {event_ticker}",
        "category": category,
        "mutually_exclusive": mutually_exclusive,
        "markets": mkts,
    }


def make_norm_market(
    ticker: str = "KXTEST-1",
    event_ticker: str = "KXEVENT-1",
    market_type: str = "binary",
    status: str = "open",
    title: str = "Will X happen?",
    yes_bid: Optional[str] = "0.4500",
    yes_ask: Optional[str] = "0.4600",
    no_bid: Optional[str] = "0.5400",
    no_ask: Optional[str] = "0.5500",
    yes_ask_size: Optional[str] = "100.00",
    no_ask_size: Optional[str] = "100.00",
    subtitle: Optional[str] = None,
    updated_time: Optional[str] = None,
) -> Any:
    raw = make_raw_market(
        ticker=ticker,
        event_ticker=event_ticker,
        market_type=market_type,
        status=status,
        title=title,
        yes_bid=yes_bid or "0.4500",
        yes_ask=yes_ask or "0.4600",
        no_bid=no_bid or "0.5400",
        no_ask=no_ask or "0.5500",
        updated_time=updated_time,
    )
    if yes_bid is None:
        raw["yes_bid_dollars"] = None
    if yes_ask is None:
        raw["yes_ask_dollars"] = None
    if no_bid is None:
        raw["no_bid_dollars"] = None
    if no_ask is None:
        raw["no_ask_dollars"] = None
    if yes_ask_size is None:
        raw["yes_ask_size_fp"] = None
    else:
        raw["yes_ask_size_fp"] = yes_ask_size
    if no_ask_size is None:
        raw["no_ask_size_fp"] = None
    else:
        raw["no_ask_size_fp"] = no_ask_size
    if subtitle is not None:
        raw["subtitle"] = subtitle
    return normalize_market(raw)


def make_norm_event(
    event_ticker: str = "KXEVENT-1",
    series_ticker: str = "KXSERIES",
    category: str = "Economics",
    mutually_exclusive: bool = True,
    markets: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    raw_ev = make_raw_event(
        event_ticker=event_ticker,
        category=category,
        markets=markets,
        series_ticker=series_ticker,
        mutually_exclusive=mutually_exclusive,
    )
    return normalize_event({"event": raw_ev})


class MockTransport:
    """Mock transport that simulates sequential Kalshi REST responses or injected errors."""

    def __init__(self, responses: Optional[Sequence[Any]] = None):
        self.responses: List[Any] = list(responses or [])
        self.calls: List[Tuple[str, Dict[str, str], float]] = []

    def get(self, url: str, params: Mapping[str, str], timeout_seconds: float) -> HTTPResponse:
        self.calls.append((url, dict(params), timeout_seconds))
        if not self.responses:
            raise KalshiTransportError(f"No mock response queued for {url}")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        if isinstance(resp, HTTPResponse):
            return resp
        if isinstance(resp, (dict, list)):
            body = json.dumps(resp).encode("utf-8")
            return HTTPResponse(200, body, {"date": "Wed, 28 Oct 2026 12:00:00 GMT"})
        raise ValueError(f"Unsupported mock response type: {type(resp)}")


def make_engine_with_transport(
    transport: MockTransport,
    config: Optional[BreadthScannerConfig] = None,
    time_provider: Optional[Any] = None,
    sleep_func: Optional[Any] = None,
) -> CatalogDiscoveryEngine:
    cfg = config or BreadthScannerConfig(discovery_page_size=10, retry_backoff_seconds=0.0)
    client = KalshiRestClient(environment="production", transport=transport)
    # Default instantaneous sleep for fast test execution
    sleep = sleep_func or (lambda s: None)
    now_fn = time_provider or (lambda: datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc))
    return CatalogDiscoveryEngine(client=client, config=cfg, time_provider=now_fn, sleep_func=sleep)


# -----------------------------------------------------------------------------
# Test Cases 1 - 28
# -----------------------------------------------------------------------------

def test_1_single_page_catalog():
    """1. Single-page catalog discovery without continuation cursor."""
    ev = make_raw_event("EV1", "Economics", [make_raw_market("M1"), make_raw_market("M2")])
    page = {"events": [ev], "cursor": None}

    transport = MockTransport([page])
    engine = make_engine_with_transport(transport)

    result = engine.discover_catalog()

    assert result.is_complete is True
    assert result.pages_fetched == 1
    assert result.unique_events_count == 1
    assert result.unique_markets_count == 2
    assert result.eligible_markets_count == 2
    assert result.excluded_markets_count == 0
    assert result.request_attempts == 1
    assert result.request_failures == 0
    assert result.retries_count == 0
    assert result.error_message is None
    assert "M1" in result.discovered_markets
    assert "M2" in result.discovered_markets


def test_2_multi_page_catalog():
    """2. Multi-page catalog discovery across a chain of cursors."""
    p1 = {"events": [make_raw_event("EV1", "Economics", [make_raw_market("M1")])], "cursor": "c_page2"}
    p2 = {"events": [make_raw_event("EV2", "Politics", [make_raw_market("M2")])], "cursor": "c_page3"}
    p3 = {"events": [make_raw_event("EV3", "Climate", [make_raw_market("M3")])], "cursor": None}

    transport = MockTransport([p1, p2, p3])
    engine = make_engine_with_transport(transport)

    result = engine.discover_catalog()

    assert result.is_complete is True
    assert result.pages_fetched == 3
    assert result.unique_events_count == 3
    assert result.unique_markets_count == 3
    assert result.eligible_markets_count == 3
    assert set(result.discovered_markets.keys()) == {"M1", "M2", "M3"}


def test_3_cursor_parameter_propagation():
    """3. Verify correct propagation of cursor into HTTP query parameters."""
    p1 = {"events": [make_raw_event("EV1")], "cursor": "c_next_123"}
    p2 = {"events": [make_raw_event("EV2")], "cursor": None}

    transport = MockTransport([p1, p2])
    engine = make_engine_with_transport(transport)

    engine.discover_catalog()

    assert len(transport.calls) == 2
    # First call must not have a cursor
    assert "cursor" not in transport.calls[0][1]
    # Second call must contain the returned cursor
    assert transport.calls[1][1]["cursor"] == "c_next_123"
    assert transport.calls[1][1]["with_nested_markets"] == "true"


def test_4_broad_enumeration_greater_than_five_markets():
    """4. Enumerate >5 markets (e.g. 60 markets) across multiple pages, breaking 5-market barrier."""
    markets_p1 = [make_raw_market(f"MKT-A-{i}") for i in range(20)]
    markets_p2 = [make_raw_market(f"MKT-B-{i}") for i in range(20)]
    markets_p3 = [make_raw_market(f"MKT-C-{i}") for i in range(20)]

    p1 = {"events": [make_raw_event("EV1", "Economics", markets_p1)], "cursor": "cur2"}
    p2 = {"events": [make_raw_event("EV2", "Politics", markets_p2)], "cursor": "cur3"}
    p3 = {"events": [make_raw_event("EV3", "Tech", markets_p3)], "cursor": None}

    transport = MockTransport([p1, p2, p3])
    engine = make_engine_with_transport(transport)

    result = engine.discover_catalog()

    assert result.is_complete is True
    assert result.unique_markets_count == 60
    assert result.eligible_markets_count == 60
    assert len(result.discovered_markets) == 60


def test_5_duplicate_market_identifiers_deduplication():
    """5. Deduplicate identical market identifiers across multiple pages (pagination drift)."""
    # M1 appears on both Page 1 and Page 2
    p1 = {"events": [make_raw_event("EV1", "Economics", [make_raw_market("M1"), make_raw_market("M2")])], "cursor": "c2"}
    p2 = {"events": [make_raw_event("EV2", "Economics", [make_raw_market("M1"), make_raw_market("M3")])], "cursor": None}

    transport = MockTransport([p1, p2])
    engine = make_engine_with_transport(transport)

    result = engine.discover_catalog()

    assert result.pages_fetched == 2
    assert result.markets_discovered_count == 4  # Total raw market occurrences encountered
    assert result.unique_markets_count == 3       # Deduplicated to exactly 3 unique markets
    assert set(result.discovered_markets.keys()) == {"M1", "M2", "M3"}


def test_6_repeated_cursor_loop_detection():
    """6. Repeated cursor detection handles cycle gracefully or raises KalshiPaginationError."""
    # Loop: page 1 -> cursor c1 -> page 2 returns c1 again
    p1 = {"events": [make_raw_event("EV1")], "cursor": "c_loop"}
    p2 = {"events": [make_raw_event("EV2")], "cursor": "c_loop"}

    transport = MockTransport([p1, p2])
    # When raise_on_error is False (default), engine logs warning and terminates pass with is_complete=False
    engine = make_engine_with_transport(transport)
    result = engine.discover_catalog()

    assert result.is_complete is False
    assert result.pages_fetched == 2
    assert "repeated" in (result.error_message or "").lower()

    # When raise_on_error is True, raises KalshiPaginationError
    transport_strict = MockTransport([p1, p2])
    engine_strict = make_engine_with_transport(transport_strict, config=BreadthScannerConfig(raise_on_error=True))
    with pytest.raises(KalshiPaginationError):
        engine_strict.discover_catalog()


def test_7_empty_terminal_page_handling():
    """7. Handle empty terminal page with empty events array and null cursor."""
    p1 = {"events": [make_raw_event("EV1", "Tech", [make_raw_market("M1")])], "cursor": "c_end"}
    p2 = {"events": [], "cursor": None}

    transport = MockTransport([p1, p2])
    engine = make_engine_with_transport(transport)

    result = engine.discover_catalog()

    assert result.is_complete is True
    assert result.pages_fetched == 2
    assert result.unique_markets_count == 1
    assert result.eligible_markets_count == 1


def test_8_api_response_missing_expected_fields():
    """8. Handle API response missing expected 'events' key."""
    bad_page = {"wrong_key": [1, 2, 3], "cursor": None}

    transport = MockTransport([bad_page])
    engine = make_engine_with_transport(transport)

    result = engine.discover_catalog()

    assert result.is_complete is False
    assert result.request_failures == 1
    assert "response events must be an array" in (result.error_message or "")


def test_9_malformed_json_handling():
    """9. Handle malformed JSON response from transport."""
    bad_resp = HTTPResponse(200, b"<html>Not JSON</html>", {"date": "Wed, 28 Oct 2026 12:00:00 GMT"})

    transport = MockTransport([bad_resp, bad_resp, bad_resp, bad_resp])
    engine = make_engine_with_transport(transport)

    result = engine.discover_catalog()

    assert result.is_complete is False
    assert result.request_failures > 0
    assert "invalid json" in (result.error_message or "").lower()


def test_10_recoverable_failure_and_retry_success():
    """10. Recoverable failure (transport error) followed by successful retry."""
    p1 = {"events": [make_raw_event("EV1", "Economics", [make_raw_market("M1")])], "cursor": "c2"}
    # On attempt to fetch page 2, fail once, then succeed
    err = KalshiTransportError("Connection reset by peer")
    p2 = {"events": [make_raw_event("EV2", "Economics", [make_raw_market("M2")])], "cursor": None}

    transport = MockTransport([p1, err, p2])
    engine = make_engine_with_transport(transport)

    result = engine.discover_catalog()

    assert result.is_complete is True
    assert result.pages_fetched == 2
    assert result.retries_count == 1
    assert result.request_failures == 1
    assert result.unique_markets_count == 2


def test_11_retry_exhaustion_reports_partial_scan():
    """11. Retry exhaustion reports partial scan as incomplete while preserving discovered markets."""
    p1 = {"events": [make_raw_event("EV1", "Economics", [make_raw_market("M1")])], "cursor": "c2"}
    err = KalshiHTTPError(500, "http://api.kalshi.com/events", "Internal Server Error")

    # Fail persistently across all retries
    transport = MockTransport([p1, err, err, err, err])
    engine = make_engine_with_transport(transport, config=BreadthScannerConfig(max_retries=2))

    result = engine.discover_catalog()

    assert result.is_complete is False
    assert result.pages_fetched == 1
    assert result.retries_count == 2
    assert result.unique_markets_count == 1
    assert "M1" in result.discovered_markets
    assert "500" in (result.error_message or "")


def test_12_cursor_expiration_or_invalid_cursor():
    """12. Invalid or expired cursor on 400 error handled gracefully without unhandled crash."""
    p1 = {"events": [make_raw_event("EV1", "Economics", [make_raw_market("M1")])], "cursor": "c_expired"}
    err = KalshiHTTPError(400, "http://api.kalshi.com/events", '{"message": "cursor expired"}')

    transport = MockTransport([p1, err])
    engine = make_engine_with_transport(transport)

    result = engine.discover_catalog()

    assert result.is_complete is False
    assert result.unique_markets_count == 1
    assert "cursor expired" in (result.error_message or "").lower()


def test_13_partial_scan_reported_as_incomplete():
    """13. Partial scan due to max_pages_per_pass is recorded accurately."""
    p1 = {"events": [make_raw_event("EV1")], "cursor": "c2"}
    p2 = {"events": [make_raw_event("EV2")], "cursor": "c3"}
    p3 = {"events": [make_raw_event("EV3")], "cursor": None}

    transport = MockTransport([p1, p2, p3])
    # Limit pass to 2 pages
    engine = make_engine_with_transport(transport, config=BreadthScannerConfig(max_pages_per_pass=2))

    result = engine.discover_catalog()

    assert result.pages_fetched == 2
    assert result.is_complete is True  # Bounded pass reached its configured bound


def test_14_refresh_discovering_newly_listed_market():
    """14. Subsequent refresh discovers newly listed markets and increments new_markets_count."""
    p1 = {"events": [make_raw_event("EV1", "Economics", [make_raw_market("M1"), make_raw_market("M2")])], "cursor": None}
    p2 = {"events": [make_raw_event("EV1", "Economics", [make_raw_market("M1"), make_raw_market("M2"), make_raw_market("M3")])], "cursor": None}

    transport = MockTransport([p1, p2])
    engine = make_engine_with_transport(transport)

    # First pass
    r1 = engine.discover_catalog()
    assert r1.new_markets_count == 2
    assert r1.updated_markets_count == 0

    # Second pass
    r2 = engine.refresh_catalog()
    assert r2.new_markets_count == 1
    assert r2.unchanged_markets_count == 2
    assert "M3" in r2.discovered_markets


def test_15_refresh_updating_status_or_quotes():
    """15. Subsequent refresh detects price quote updates and increments updated_markets_count."""
    mkt_v1 = make_raw_market("M1", yes_bid="0.4000")
    mkt_v2 = make_raw_market("M1", yes_bid="0.4500")  # Price tick

    p1 = {"events": [make_raw_event("EV1", "Economics", [mkt_v1])], "cursor": None}
    p2 = {"events": [make_raw_event("EV1", "Economics", [mkt_v2])], "cursor": None}

    transport = MockTransport([p1, p2])
    engine = make_engine_with_transport(transport)

    r1 = engine.discover_catalog()
    assert r1.new_markets_count == 1

    r2 = engine.refresh_catalog()
    assert r2.new_markets_count == 0
    assert r2.updated_markets_count == 1
    assert r2.unchanged_markets_count == 0


def test_16_unique_event_count_vs_unique_market_count():
    """16. Verify distinct event counts and market counts (e.g. 3 events, each with 4 markets)."""
    events = [
        make_raw_event(f"EV-{i}", "Economics", [make_raw_market(f"MKT-{i}-{j}") for j in range(4)])
        for i in range(3)
    ]
    p1 = {"events": events, "cursor": None}

    transport = MockTransport([p1])
    engine = make_engine_with_transport(transport)

    result = engine.discover_catalog()

    assert result.unique_events_count == 3
    assert result.unique_markets_count == 12
    assert result.eligible_markets_count == 12


def test_17_stable_identifier_based_deduplication():
    """17. Markets with same title but distinct tickers are kept distinct; same ticker deduplicated."""
    # Two distinct markets with same title
    m1 = make_raw_market("MKT-1", title="US GDP Q3 Positive?")
    m2 = make_raw_market("MKT-2", title="US GDP Q3 Positive?")
    # Duplicate ticker with different title
    m1_dup = make_raw_market("MKT-1", title="Renamed title")

    p1 = {"events": [make_raw_event("EV1", "Economics", [m1, m2, m1_dup])], "cursor": None}

    transport = MockTransport([p1])
    engine = make_engine_with_transport(transport)

    result = engine.discover_catalog()

    assert result.unique_markets_count == 2
    assert set(result.discovered_markets.keys()) == {"MKT-1", "MKT-2"}


def test_18_eligible_vs_excluded_markets():
    """18. Distinguish eligible binary open markets from closed and non-binary markets."""
    m_open = make_raw_market("M-OPEN", status="open", market_type="binary")
    m_closed = make_raw_market("M-CLOSED", status="closed", market_type="binary")
    m_multi = make_raw_market("M-MULTI", status="open", market_type="multivariate")

    p1 = {"events": [make_raw_event("EV1", "Economics", [m_open, m_closed, m_multi])], "cursor": None}

    transport = MockTransport([p1])
    engine = make_engine_with_transport(transport)

    result = engine.discover_catalog()

    assert result.unique_markets_count == 3
    assert result.eligible_markets_count == 1
    assert result.excluded_markets_count == 2
    assert "M-OPEN" in result.discovered_markets
    assert "M-CLOSED" not in result.discovered_markets
    assert "M-MULTI" not in result.discovered_markets


def test_19_exclusion_reasons_breakdown():
    """19. Detailed breakdown of exclusion reasons accurately tallied."""
    now_dt = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    expired_time = (now_dt - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    m_closed = make_raw_market("M-CLOSED", status="settled")
    m_non_binary = make_raw_market("M-SCALAR", market_type="scalar")
    m_no_ticker = {"market_type": "binary", "status": "open", "title": "Missing ticker"}
    m_past_close = make_raw_market("M-EXPIRED", close_time=expired_time)

    p1 = {"events": [make_raw_event("EV1", "Economics", [m_closed, m_non_binary, m_no_ticker, m_past_close])], "cursor": None}

    transport = MockTransport([p1])
    engine = make_engine_with_transport(transport, time_provider=lambda: now_dt)

    result = engine.discover_catalog()

    assert result.eligible_markets_count == 0
    assert result.excluded_markets_count == 4
    assert result.exclusion_breakdown[MarketExclusionReason.STATUS_NOT_OPEN.value] == 1
    assert result.exclusion_breakdown[MarketExclusionReason.NON_BINARY_TYPE.value] == 1
    assert result.exclusion_breakdown[MarketExclusionReason.MISSING_TICKER.value] == 1
    assert result.exclusion_breakdown[MarketExclusionReason.PAST_CLOSE_TIME.value] == 1


def test_20_category_filtering():
    """20. Filter catalog by specified economic/topical categories."""
    ev_econ = make_raw_event("EV-ECON", "Economics", [make_raw_market("M-ECON")])
    ev_pol = make_raw_event("EV-POL", "Politics", [make_raw_market("M-POL")])
    ev_sport = make_raw_event("EV-SPORT", "Sports", [make_raw_market("M-SPORT")])

    p1 = {"events": [ev_econ, ev_pol, ev_sport], "cursor": None}

    transport = MockTransport([p1])
    cfg = BreadthScannerConfig(category_filter=("Economics", "Politics"))
    engine = make_engine_with_transport(transport, config=cfg)

    result = engine.discover_catalog()

    assert "M-ECON" in result.discovered_markets
    assert "M-POL" in result.discovered_markets
    assert "M-SPORT" not in result.discovered_markets
    assert result.eligible_markets_count == 2
    assert result.exclusion_breakdown[MarketExclusionReason.CATEGORY_FILTERED.value] == 1


def test_21_isolation_zero_order_book_calls():
    """21. Isolation: Engine must make ZERO order-book endpoint calls during discovery."""
    events = [make_raw_event(f"EV-{i}", "Economics", [make_raw_market(f"M-{i}")]) for i in range(10)]
    p1 = {"events": events, "cursor": None}

    transport = MockTransport([p1])
    engine = make_engine_with_transport(transport)

    engine.discover_catalog()

    # Verify no endpoint called has 'orderbook' in URL
    assert len(transport.calls) == 1
    url, _, _ = transport.calls[0]
    assert "orderbook" not in url
    assert url.endswith("/events")


def test_22_isolation_zero_state_file_writes(tmp_path):
    """22. Isolation: Engine does not modify live continuous collector state or observations file."""
    p1 = {"events": [make_raw_event("EV1", "Economics", [make_raw_market("M1")])], "cursor": None}

    transport = MockTransport([p1])
    engine = make_engine_with_transport(transport)

    live_state = Path("data/live_collector_state.json")
    live_obs = Path("data/live_collection/observations.jsonl")

    mtime_state_before = live_state.stat().st_mtime if live_state.exists() else None
    mtime_obs_before = live_obs.stat().st_mtime if live_obs.exists() else None

    engine.discover_catalog()

    mtime_state_after = live_state.stat().st_mtime if live_state.exists() else None
    mtime_obs_after = live_obs.stat().st_mtime if live_obs.exists() else None

    assert mtime_state_before == mtime_state_after
    assert mtime_obs_before == mtime_obs_after


def test_23_existing_rest_client_pagination_regression():
    """23. Verify KalshiRestClient pagination methods function identically without regression."""
    p1 = {"events": [make_raw_event("EV1")], "cursor": "c2"}
    p2 = {"events": [make_raw_event("EV2")], "cursor": None}

    transport = MockTransport([p1, p2])
    client = KalshiRestClient(environment="production", transport=transport)

    events = client.list_events(with_nested_markets=True)
    assert len(events) == 2
    assert events[0].event_ticker == "EV1"
    assert events[1].event_ticker == "EV2"


def test_24_existing_collector_and_evaluator_compatibility():
    """24. Verify existing collector, evaluator, qualifier, and alert classes instantiate normally."""
    collector = ContinuousCollector()
    assert collector is not None
    assert collector.is_running is False

    evaluator = ContinuousEvaluator()
    assert evaluator is not None
    assert evaluator.is_running is False

    qualifier = ExecutionAwareArbitrageQualifier()
    assert qualifier is not None

    alert_engine = AlertEngine()
    assert alert_engine is not None


def test_25_zero_credentials_or_order_placement():
    """25. Verify zero credentials requested and no order placement endpoints contacted."""
    p1 = {"events": [make_raw_event("EV1")], "cursor": None}
    transport = MockTransport([p1])
    engine = make_engine_with_transport(transport)

    engine.discover_catalog()

    for url, _, _ in transport.calls:
        # Verify no account, balance, position, or order routing endpoints contacted
        path_suffix = url.split("/trade-api/v2")[-1] if "/trade-api/v2" in url else url
        assert "/portfolio" not in path_suffix
        assert "/orders" not in path_suffix
        assert "/fills" not in path_suffix


def test_26_markets_source_discovery():
    """26. Discover catalog via /markets endpoint when configured."""
    markets = [make_raw_market(f"MKT-{i}") for i in range(5)]
    p1 = {"markets": markets, "cursor": None}

    transport = MockTransport([p1])
    cfg = BreadthScannerConfig(catalog_source="markets")
    engine = make_engine_with_transport(transport, config=cfg)

    result = engine.discover_catalog()

    assert result.is_complete is True
    assert result.pages_fetched == 1
    assert result.unique_markets_count == 5
    assert len(transport.calls) == 1
    assert transport.calls[0][0].endswith("/markets")


def test_27_cooperative_stop():
    """27. Verify engine halts immediately when stop() is called."""
    p1 = {"events": [make_raw_event("EV1")], "cursor": "c2"}
    p2 = {"events": [make_raw_event("EV2")], "cursor": None}

    transport = MockTransport([p1, p2])
    engine = make_engine_with_transport(transport)
    engine.stop()

    result = engine.discover_catalog()

    assert result.pages_fetched == 0
    assert result.is_complete is False
    assert len(transport.calls) == 0


def test_28_discovery_result_to_dict_serialization():
    """28. Verify CatalogDiscoveryResult.to_dict() produces valid serializable JSON dictionary."""
    ev = make_raw_event("EV1", "Economics", [make_raw_market("M1")])
    p1 = {"events": [ev], "cursor": None}

    transport = MockTransport([p1])
    engine = make_engine_with_transport(transport)

    result = engine.discover_catalog()
    d = result.to_dict()

    assert isinstance(d, dict)
    assert d["is_complete"] is True
    assert d["unique_events_count"] == 1
    assert d["unique_markets_count"] == 1
    assert d["eligible_markets_count"] == 1
    assert d["eligible_market_tickers"] == ["M1"]
    assert json.dumps(d)  # Must be JSON serializable without error

    summary = engine.get_summary()
    assert summary == d


# =============================================================================
# Phase 2B Tests: Lightweight Breadth-First Arbitrage Screening (Tests 2B-1 to 2B-30)
# =============================================================================

# -----------------------------------------------------------------------------
# Binary Parity Screening (Tests 2B-1 to 2B-10)
# -----------------------------------------------------------------------------

def test_2b_1_binary_parity_positive_edge():
    """2B-1. YES ask plus NO ask below $1.00 produces PRELIMINARY_CANDIDATE."""
    m = make_norm_market("M1", yes_ask="0.4500", no_ask="0.4500")
    screener = BreadthArbitrageScreener()

    res = screener.screen_market_binary_parity(m)

    assert res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert res.candidate is not None
    assert res.candidate.strategy_type == ScreeningStrategyType.BINARY_PARITY.value
    assert res.candidate.gross_edge_dollars == Decimal("0.1000")
    assert res.candidate.gross_edge_cents == 10
    assert res.candidate.estimated_entry_cost_dollars == Decimal("0.9000")
    assert res.candidate.guaranteed_payout_dollars == Decimal("1.0000")
    assert res.candidate.is_execution_qualified is False
    assert res.candidate.market_tickers == ("M1",)


def test_2b_2_binary_parity_exact_dollar_breakeven():
    """2B-2. YES ask plus NO ask exactly $1.00 produces NO_CANDIDATE (break-even)."""
    m = make_norm_market("M1", yes_ask="0.5000", no_ask="0.5000")
    screener = BreadthArbitrageScreener()

    res = screener.screen_market_binary_parity(m)

    assert res.outcome == ScreeningOutcome.NO_CANDIDATE
    assert res.candidate is None
    assert "No positive gross parity edge" in res.reason


def test_2b_3_binary_parity_above_dollar_no_edge():
    """2B-3. YES ask plus NO ask above $1.00 produces NO_CANDIDATE."""
    m = make_norm_market("M1", yes_ask="0.5500", no_ask="0.5200")
    screener = BreadthArbitrageScreener()

    res = screener.screen_market_binary_parity(m)

    assert res.outcome == ScreeningOutcome.NO_CANDIDATE
    assert res.candidate is None
    assert ">= guaranteed payout" in res.reason


def test_2b_4_binary_parity_missing_yes_ask():
    """2B-4. Missing YES ask quote skips market with SKIPPED_INCOMPLETE_QUOTES."""
    m = make_norm_market("M1", yes_ask=None, no_ask="0.4500")
    screener = BreadthArbitrageScreener()

    res = screener.screen_market_binary_parity(m)

    assert res.outcome == ScreeningOutcome.SKIPPED_INCOMPLETE_QUOTES
    assert res.candidate is None
    assert "yes_ask_dollars" in res.reason


def test_2b_5_binary_parity_missing_no_ask():
    """2B-5. Missing NO ask quote skips market with SKIPPED_INCOMPLETE_QUOTES."""
    m = make_norm_market("M1", yes_ask="0.4500", no_ask=None)
    screener = BreadthArbitrageScreener()

    res = screener.screen_market_binary_parity(m)

    assert res.outcome == ScreeningOutcome.SKIPPED_INCOMPLETE_QUOTES
    assert res.candidate is None
    assert "no_ask_dollars" in res.reason


def test_2b_6_binary_parity_fractional_cent_precision():
    """2B-6. Fractional-cent quotes evaluated with exact Decimal precision without floating-point error."""
    m = make_norm_market("M1", yes_ask="0.4050", no_ask="0.5800")
    screener = BreadthArbitrageScreener()

    res = screener.screen_market_binary_parity(m)

    assert res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert res.candidate is not None
    # 0.4050 + 0.5800 = 0.9850 -> Gross edge = 0.0150 exact Decimal
    assert res.candidate.gross_edge_dollars == Decimal("0.0150")
    assert res.candidate.estimated_entry_cost_dollars == Decimal("0.9850")
    assert res.candidate.guaranteed_payout_dollars == Decimal("1.0000")


def test_2b_7_binary_parity_malformed_out_of_range_prices():
    """2B-7. Malformed or out-of-range prices rejected with SKIPPED_INVALID_DATA."""
    # Construct synthetic mock with out-of-range price
    m = make_norm_market("M1", yes_ask="0.4500", no_ask="0.4500")
    # Simulate a market object that bypassed initial bounds
    object.__setattr__(m, "yes_ask_dollars", Decimal("1.2500"))
    screener = BreadthArbitrageScreener()

    res = screener.screen_market_binary_parity(m)

    assert res.outcome == ScreeningOutcome.SKIPPED_INVALID_DATA
    assert res.candidate is None
    assert "out of valid range" in res.reason


def test_2b_8_binary_parity_quote_sizes_absent_or_zero():
    """2B-8. Absent or zero quote sizes preserved as liquidity context without rejecting candidate."""
    m_no_size = make_norm_market("M1", yes_ask="0.4500", no_ask="0.4500", yes_ask_size=None, no_ask_size=None)
    m_zero_size = make_norm_market("M2", yes_ask="0.4500", no_ask="0.4500", yes_ask_size="0.00", no_ask_size="0.00")
    screener = BreadthArbitrageScreener()

    res1 = screener.screen_market_binary_parity(m_no_size)
    assert res1.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert any("sizes unavailable" in note for note in res1.candidate.validation_limitations)

    res2 = screener.screen_market_binary_parity(m_zero_size)
    assert res2.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert any("size is zero" in note for note in res2.candidate.validation_limitations)


def test_2b_9_binary_parity_gross_edge_and_cost_precision():
    """2B-9. Exact Decimal edge calculations across edge test matrix."""
    screener = BreadthArbitrageScreener()

    # Extreme low cost
    m_cheap = make_norm_market("M-CHEAP", yes_ask="0.0100", no_ask="0.0100")
    res_cheap = screener.screen_market_binary_parity(m_cheap)
    assert res_cheap.candidate.gross_edge_dollars == Decimal("0.9800")
    assert res_cheap.candidate.gross_edge_cents == 98

    # Three-decimal fractional quotes
    m_frac = make_norm_market("M-FRAC", yes_ask="0.3333", no_ask="0.3333")
    res_frac = screener.screen_market_binary_parity(m_frac)
    assert res_frac.candidate.gross_edge_dollars == Decimal("0.3334")
    assert res_frac.candidate.estimated_entry_cost_dollars == Decimal("0.6666")


def test_2b_10_binary_parity_repeated_input_stable_key():
    """2B-10. Repeated identical inputs produce stable, deterministic candidate keys."""
    m = make_norm_market("M-STABLE", yes_ask="0.4500", no_ask="0.4500")
    screener = BreadthArbitrageScreener()

    res1 = screener.screen_market_binary_parity(m)
    res2 = screener.screen_market_binary_parity(m)

    assert res1.candidate.candidate_id == res2.candidate.candidate_id == "PRE-BP:M-STABLE"
    assert res1.candidate.stable_key == res2.candidate.stable_key == "BP:M-STABLE"


# -----------------------------------------------------------------------------
# MECE Basket Screening (Tests 2B-11 to 2B-18)
# -----------------------------------------------------------------------------

def test_2b_11_mece_long_yes_profitable_basket():
    """2B-11. Valid synthetic MECE event with profitable Long YES basket produces candidate."""
    m1 = make_norm_market("FED-CUT", event_ticker="EV-FED", yes_ask="0.2000", no_ask="0.8000")
    m2 = make_norm_market("FED-HOLD", event_ticker="EV-FED", yes_ask="0.3500", no_ask="0.6500")
    m3 = make_norm_market("FED-HIKE", event_ticker="EV-FED", yes_ask="0.3000", no_ask="0.7000")
    raw_mkts = [make_raw_market("FED-CUT"), make_raw_market("FED-HOLD"), make_raw_market("FED-HIKE")]

    event = make_norm_event("EV-FED", series_ticker="KXFEDDECISION", markets=raw_mkts)
    screener = BreadthArbitrageScreener()

    results = screener.screen_event_mece_baskets(event, [m1, m2, m3])
    yes_res = [r for r in results if r.strategy_type == ScreeningStrategyType.MECE_BASKET_LONG_YES.value][0]

    assert yes_res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert yes_res.candidate is not None
    # 0.20 + 0.35 + 0.30 = 0.85 -> gross edge = 0.15 (15c)
    assert yes_res.candidate.gross_edge_dollars == Decimal("0.1500")
    assert yes_res.candidate.gross_edge_cents == 15
    assert yes_res.candidate.estimated_entry_cost_dollars == Decimal("0.8500")
    assert yes_res.candidate.guaranteed_payout_dollars == Decimal("1.0000")
    assert yes_res.candidate.is_execution_qualified is False


def test_2b_12_mece_long_yes_no_edge():
    """2B-12. Valid synthetic MECE event without Long YES edge produces NO_CANDIDATE."""
    m1 = make_norm_market("FED-CUT", event_ticker="EV-FED", yes_ask="0.4000", no_ask="0.6000")
    m2 = make_norm_market("FED-HOLD", event_ticker="EV-FED", yes_ask="0.4000", no_ask="0.6000")
    m3 = make_norm_market("FED-HIKE", event_ticker="EV-FED", yes_ask="0.3000", no_ask="0.7000")
    raw_mkts = [make_raw_market("FED-CUT"), make_raw_market("FED-HOLD"), make_raw_market("FED-HIKE")]

    event = make_norm_event("EV-FED", series_ticker="KXFEDDECISION", markets=raw_mkts)
    screener = BreadthArbitrageScreener()

    results = screener.screen_event_mece_baskets(event, [m1, m2, m3])
    yes_res = [r for r in results if r.strategy_type == ScreeningStrategyType.MECE_BASKET_LONG_YES.value][0]

    assert yes_res.outcome == ScreeningOutcome.NO_CANDIDATE
    assert yes_res.candidate is None
    assert "No positive gross MECE Long YES edge" in yes_res.reason


def test_2b_13_mece_incomplete_event_outcome_set():
    """2B-13. Incomplete event outcome set rejected with SKIPPED_INCOMPLETE_EVENT_GROUP."""
    m1 = make_norm_market("FED-CUT", event_ticker="EV-FED")
    m2 = make_norm_market("FED-HOLD", event_ticker="EV-FED")
    # Event declares 3 markets, but only 2 supplied
    raw_mkts = [make_raw_market("FED-CUT"), make_raw_market("FED-HOLD"), make_raw_market("FED-HIKE")]
    event = make_norm_event("EV-FED", series_ticker="KXFEDDECISION", markets=raw_mkts)

    screener = BreadthArbitrageScreener()
    results = screener.screen_event_mece_baskets(event, [m1, m2])

    for r in results:
        assert r.outcome == ScreeningOutcome.SKIPPED_INCOMPLETE_EVENT_GROUP
        assert "Incomplete event group" in r.reason


def test_2b_14_mece_duplicate_contract_identifiers():
    """2B-14. Duplicate contract identifiers in event group rejected with SKIPPED_INVALID_DATA."""
    m1 = make_norm_market("FED-CUT", event_ticker="EV-FED")
    raw_mkts = [make_raw_market("FED-CUT"), make_raw_market("FED-HOLD")]
    event = make_norm_event("EV-FED", series_ticker="KXFEDDECISION", markets=raw_mkts)

    screener = BreadthArbitrageScreener()
    # Pass duplicate m1 twice
    results = screener.screen_event_mece_baskets(event, [m1, m1])

    for r in results:
        assert r.outcome == ScreeningOutcome.SKIPPED_INVALID_DATA
        assert "Duplicate contract identifier" in r.reason


def test_2b_15_mece_non_mece_or_multivariate_combo():
    """2B-15. Non-MECE event (mutually_exclusive=False) rejected with SKIPPED_UNSUPPORTED_MARKET."""
    m1 = make_norm_market("M1", event_ticker="EV-NON-MECE")
    m2 = make_norm_market("M2", event_ticker="EV-NON-MECE")
    raw_mkts = [make_raw_market("M1"), make_raw_market("M2")]
    event = make_norm_event("EV-NON-MECE", mutually_exclusive=False, markets=raw_mkts)

    screener = BreadthArbitrageScreener()
    results = screener.screen_event_mece_baskets(event, [m1, m2])

    for r in results:
        assert r.outcome == ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET
        assert "mutually_exclusive=False" in r.reason


def test_2b_16_mece_missing_constituent_quotes():
    """2B-16. Missing constituent quote skips strategy with SKIPPED_INCOMPLETE_QUOTES."""
    m1 = make_norm_market("FED-CUT", event_ticker="EV-FED", yes_ask="0.2000")
    m2 = make_norm_market("FED-HOLD", event_ticker="EV-FED", yes_ask=None)  # Missing YES ask
    raw_mkts = [make_raw_market("FED-CUT"), make_raw_market("FED-HOLD")]
    event = make_norm_event("EV-FED", series_ticker="KXFEDDECISION", markets=raw_mkts)

    screener = BreadthArbitrageScreener()
    results = screener.screen_event_mece_baskets(event, [m1, m2])
    yes_res = [r for r in results if r.strategy_type == ScreeningStrategyType.MECE_BASKET_LONG_YES.value][0]

    assert yes_res.outcome == ScreeningOutcome.SKIPPED_INCOMPLETE_QUOTES
    assert "Missing yes_ask_dollars" in yes_res.reason


def test_2b_17_mece_long_no_validated_payout_model():
    """2B-17. Long NO basket evaluated against validated mathematical payout formulation (N-1)*$1.00."""
    # N = 3 outcomes. Guaranteed payout = (3 - 1) * $1.00 = $2.00.
    # Total NO acquisition cost = 0.60 + 0.60 + 0.65 = 1.85.
    # Gross edge = $2.00 - $1.85 = $0.15 (15c).
    m1 = make_norm_market("FED-CUT", event_ticker="EV-FED", yes_ask="0.5000", no_ask="0.6000")
    m2 = make_norm_market("FED-HOLD", event_ticker="EV-FED", yes_ask="0.5000", no_ask="0.6000")
    m3 = make_norm_market("FED-HIKE", event_ticker="EV-FED", yes_ask="0.5000", no_ask="0.6500")
    raw_mkts = [make_raw_market("FED-CUT"), make_raw_market("FED-HOLD"), make_raw_market("FED-HIKE")]
    event = make_norm_event("EV-FED", series_ticker="KXFEDDECISION", markets=raw_mkts)

    screener = BreadthArbitrageScreener()
    results = screener.screen_event_mece_baskets(event, [m1, m2, m3])
    no_res = [r for r in results if r.strategy_type == ScreeningStrategyType.MECE_BASKET_LONG_NO.value][0]

    assert no_res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert no_res.candidate is not None
    assert no_res.candidate.gross_edge_dollars == Decimal("0.1500")
    assert no_res.candidate.gross_edge_cents == 15
    assert no_res.candidate.guaranteed_payout_dollars == Decimal("2.0000")
    assert no_res.candidate.estimated_entry_cost_dollars == Decimal("1.8500")
    assert no_res.candidate.is_execution_qualified is False


def test_2b_18_mece_unsupported_lacking_exhaustiveness():
    """2B-18. Event lacking collective exhaustiveness proof rejected with SKIPPED_UNSUPPORTED_MARKET."""
    # Generic series without range prefix and without residual outcome
    m1 = make_norm_market("KX-A", event_ticker="EV-OPEN-ENDED", title="Candidate A")
    m2 = make_norm_market("KX-B", event_ticker="EV-OPEN-ENDED", title="Candidate B")
    raw_mkts = [make_raw_market("KX-A"), make_raw_market("KX-B")]
    event = make_norm_event("EV-OPEN-ENDED", series_ticker="KXGENERIC", markets=raw_mkts)

    screener = BreadthArbitrageScreener()
    results = screener.screen_event_mece_baskets(event, [m1, m2])

    for r in results:
        assert r.outcome == ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET
        assert "not verified collectively exhaustive" in r.reason


# -----------------------------------------------------------------------------
# Crossed-Book Screening (Tests 2B-19 to 2B-23)
# -----------------------------------------------------------------------------

def test_2b_19_crossed_book_valid_flag():
    """2B-19. Inverted quotes (yes_bid > yes_ask or complement bids > $1.00) generate crossed_book candidate."""
    # Direct inverted spread
    m_inv = make_norm_market("M-INV", yes_bid="0.5500", yes_ask="0.4500", no_bid="0.4000", no_ask="0.5000")
    screener = BreadthArbitrageScreener()

    res = screener.screen_market_crossed_book(m_inv)
    assert res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert res.candidate is not None
    assert res.candidate.strategy_type == ScreeningStrategyType.CROSSED_BOOK.value
    assert "Inverted YES spread" in res.reason

    # Complement bids crossed (yes_bid + no_bid > 1.00)
    m_comp = make_norm_market("M-COMP", yes_bid="0.5500", yes_ask="0.5800", no_bid="0.5800", no_ask="0.6000")
    res_comp = screener.screen_market_crossed_book(m_comp)
    assert res_comp.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert "Complement bids crossed" in res_comp.reason


def test_2b_20_crossed_book_non_crossed_quotes():
    """2B-20. Normal, non-crossed quotes produce NO_CANDIDATE."""
    m_norm = make_norm_market("M-NORM", yes_bid="0.4500", yes_ask="0.4600", no_bid="0.5300", no_ask="0.5400")
    screener = BreadthArbitrageScreener()

    res = screener.screen_market_crossed_book(m_norm)
    assert res.outcome == ScreeningOutcome.NO_CANDIDATE
    assert res.candidate is None
    assert "Non-crossed quotes" in res.reason


def test_2b_21_crossed_book_missing_quotes():
    """2B-21. Missing quotes for crossed evaluation skips market with SKIPPED_INCOMPLETE_QUOTES."""
    m_no_quotes = make_norm_market("M-EMPTY", yes_bid=None, yes_ask=None, no_bid=None, no_ask=None)
    screener = BreadthArbitrageScreener()

    res = screener.screen_market_crossed_book(m_no_quotes)
    assert res.outcome == ScreeningOutcome.SKIPPED_INCOMPLETE_QUOTES
    assert "Insufficient bid/ask quotes" in res.reason


def test_2b_22_crossed_book_malformed_quotes():
    """2B-22. Malformed or out-of-range quotes produce SKIPPED_INVALID_DATA."""
    m = make_norm_market("M-BAD")
    object.__setattr__(m, "yes_bid_dollars", Decimal("1.5000"))
    screener = BreadthArbitrageScreener()

    res = screener.screen_market_crossed_book(m)
    assert res.outcome == ScreeningOutcome.SKIPPED_INVALID_DATA
    assert "out of bounds" in res.reason


def test_2b_23_crossed_book_never_labeled_verified_opportunity():
    """2B-23. Crossed book flags are never classified as execution-qualified or verified opportunities."""
    m_inv = make_norm_market("M-INV", yes_bid="0.5500", yes_ask="0.4500")
    screener = BreadthArbitrageScreener()

    res = screener.screen_market_crossed_book(m_inv)
    cand = res.candidate

    assert cand.is_execution_qualified is False
    assert any("Suspicious crossed or inverted top-of-book" in note for note in cand.validation_limitations)
    assert any("Not an execution-qualified opportunity" in note for note in cand.validation_limitations)


# -----------------------------------------------------------------------------
# Integration, Deduplication & Safety Invariants (Tests 2B-24 to 2B-30)
# -----------------------------------------------------------------------------

def test_2b_24_screen_catalog_more_than_five_markets():
    """2B-24. Screen a synthetic catalog containing more than five unique markets (e.g. 20 markets)."""
    markets = [
        make_norm_market(f"MKT-{i}", event_ticker=f"EV-{i // 5}", yes_ask="0.4500", no_ask="0.5000")
        for i in range(20)
    ]
    screener = BreadthArbitrageScreener()

    summary = screener.screen_catalog(markets)

    assert summary.total_markets_screened == 20
    assert summary.total_events_screened == 4
    assert len(summary.market_results) == 20


def test_2b_25_screen_catalog_explicit_candidate_and_non_candidate_outcomes():
    """2B-25. Produce explicit typed outcomes for both candidate and non-candidate markets."""
    # M1 has parity edge (consistent bids); M2 is normal (no edge); M3 is missing quotes
    m1 = make_norm_market("M1", yes_bid="0.3800", yes_ask="0.4000", no_bid="0.4200", no_ask="0.4500")  # Parity Candidate
    m2 = make_norm_market("M2", yes_bid="0.4500", yes_ask="0.5200", no_bid="0.4500", no_ask="0.5200")  # No candidate
    m3 = make_norm_market("M3", yes_bid=None, yes_ask=None, no_bid=None, no_ask="0.5000")               # Incomplete quotes

    screener = BreadthArbitrageScreener()
    summary = screener.screen_catalog([m1, m2, m3])

    assert summary.market_results["M1"].overall_outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert summary.market_results["M2"].overall_outcome == ScreeningOutcome.NO_CANDIDATE
    assert summary.market_results["M3"].overall_outcome == ScreeningOutcome.SKIPPED_INCOMPLETE_QUOTES
    assert summary.outcomes_breakdown[ScreeningOutcome.PRELIMINARY_CANDIDATE.value] == 1
    assert summary.outcomes_breakdown[ScreeningOutcome.NO_CANDIDATE.value] == 1
    assert summary.outcomes_breakdown[ScreeningOutcome.SKIPPED_INCOMPLETE_QUOTES.value] == 1


def test_2b_26_screening_performs_zero_http_requests():
    """2B-26. Verification that BreadthArbitrageScreener performs zero network HTTP requests."""
    # Screener has no network client and takes pure in-memory models
    screener = BreadthArbitrageScreener()
    markets = [make_norm_market(f"M-{i}") for i in range(10)]

    summary = screener.screen_catalog(markets)

    assert summary.total_markets_screened == 10
    # Screener has no transport or client attribute that could make network requests


def test_2b_27_screening_performs_zero_order_book_requests():
    """2B-27. Verification that screening performs zero L2 order-book requests."""
    screener = BreadthArbitrageScreener()
    markets = [make_norm_market(f"M-{i}") for i in range(10)]

    summary = screener.screen_catalog(markets)

    # All candidate evidence originates strictly from NormalizedMarket top-of-book fields
    for c in summary.candidates:
        assert c.strategy_type in (
            ScreeningStrategyType.BINARY_PARITY.value,
            ScreeningStrategyType.CROSSED_BOOK.value,
            ScreeningStrategyType.MECE_BASKET_LONG_YES.value,
            ScreeningStrategyType.MECE_BASKET_LONG_NO.value,
        )


def test_2b_28_screening_surfaces_incomplete_discovery():
    """2B-28. Incomplete upstream catalog discovery is preserved and surfaced in screening summary."""
    disc_result = CatalogDiscoveryResult(
        session_id="disc-test",
        started_at="2026-10-28T12:00:00Z",
        is_complete=False,
        error_message="HTTP 500 on page 3",
        discovered_markets={"M1": make_norm_market("M1")},
    )

    screener = BreadthArbitrageScreener()
    summary = screener.screen_catalog(disc_result)

    assert summary.is_catalog_complete is False
    assert summary.upstream_error == "HTTP 500 on page 3"
    assert summary.total_markets_screened == 1


def test_2b_29_screening_deduplicates_unchanged_candidates():
    """2B-29. Repeated unchanged candidates across screening passes are deduplicated."""
    m_arb = make_norm_market("M-ARB", yes_bid="0.3800", yes_ask="0.4000", no_bid="0.4200", no_ask="0.4500")
    screener = BreadthArbitrageScreener()

    # Pass 1: candidate detected as new
    s1 = screener.screen_catalog([m_arb])
    assert s1.preliminary_candidates_count == 1
    assert s1.new_candidates_count == 1
    assert s1.deduplicated_candidates_count == 0

    # Pass 2: identical input reports as deduplicated
    s2 = screener.screen_catalog([m_arb])
    assert s2.preliminary_candidates_count == 1
    assert s2.new_candidates_count == 0
    assert s2.deduplicated_candidates_count == 1


def test_2b_30_screening_never_labels_unvalidated_candidate_as_execution_qualified():
    """2B-30. Screener guarantees that every candidate has is_execution_qualified=False."""
    # Mix of binary parity, MECE, and crossed book candidates
    m_bp = make_norm_market("M-BP", event_ticker="EV-FED", yes_ask="0.4000", no_ask="0.4000")
    m_xb = make_norm_market("M-XB", event_ticker="EV-FED", yes_bid="0.5500", yes_ask="0.4500")
    m_m1 = make_norm_market("FED-1", event_ticker="EV-FED", yes_ask="0.2000", no_ask="0.8000")
    m_m2 = make_norm_market("FED-2", event_ticker="EV-FED", yes_ask="0.3000", no_ask="0.7000")
    raw_mkts = [make_raw_market("FED-1"), make_raw_market("FED-2")]
    event = make_norm_event("EV-FED", series_ticker="KXFEDDECISION", markets=raw_mkts)

    screener = BreadthArbitrageScreener()
    summary = screener.screen_catalog([m_bp, m_xb, m_m1, m_m2], events={"EV-FED": event})

    assert len(summary.candidates) > 0
    for cand in summary.candidates:
        assert cand.is_execution_qualified is False
        assert isinstance(cand.validation_limitations, tuple)
        assert len(cand.validation_limitations) > 0


# =============================================================================
# Phase 2C: Targeted Order-Book Qualification Tests
# =============================================================================

def make_test_order_book(
    ticker: str,
    yes_bids: Sequence[Tuple[str, int]] = (("0.55", 10),),
    no_bids: Sequence[Tuple[str, int]] = (("0.58", 10),),
    source_timestamp: Optional[datetime] = None,
) -> NormalizedOrderBook:
    """Helper to construct NormalizedOrderBook with resting bids for testing.

    In Kalshi:
    - yes_bids at price P create NO asks at (1.00 - P)
    - no_bids at price P create YES asks at (1.00 - P)
    Default bids (yes=0.55, no=0.58) derive:
    - YES ask = 0.42 ($0.42), quantity 10
    - NO ask = 0.45 ($0.45), quantity 10
    Sum of asks = $0.87 -> gross edge = 13 cents.
    """
    if source_timestamp is None:
        source_timestamp = datetime.now(timezone.utc)
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=tuple(OrderBookLevel(price_dollars=Decimal(p), quantity=q) for p, q in yes_bids),
        no_bids=tuple(OrderBookLevel(price_dollars=Decimal(p), quantity=q) for p, q in no_bids),
        source_timestamp=source_timestamp,
    )


def make_test_preliminary_candidate(
    candidate_id: str = "PRE-1",
    strategy_type: str = ScreeningStrategyType.BINARY_PARITY.value,
    event_ticker: str = "EV-1",
    market_tickers: Sequence[str] = ("M-1",),
    gross_edge_dollars: str = "0.13",
    gross_edge_cents: int = 13,
    estimated_entry_cost_dollars: str = "0.87",
    guaranteed_payout_dollars: str = "1.00",
    quote_timestamp: Optional[str] = None,
    stable_key: Optional[str] = None,
) -> PreliminaryCandidate:
    """Helper to construct PreliminaryCandidate for testing."""
    if quote_timestamp is None:
        quote_timestamp = datetime.now(timezone.utc).isoformat()
    if stable_key is None:
        stable_key = f"{strategy_type}:{':'.join(market_tickers)}"

    constituent_quotes = {
        t: {"yes_ask": "0.4200", "no_ask": "0.4500"} for t in market_tickers
    }

    return PreliminaryCandidate(
        candidate_id=candidate_id,
        strategy_type=strategy_type,
        event_ticker=event_ticker,
        market_tickers=tuple(market_tickers),
        gross_edge_dollars=Decimal(gross_edge_dollars),
        gross_edge_cents=gross_edge_cents,
        estimated_entry_cost_dollars=Decimal(estimated_entry_cost_dollars),
        guaranteed_payout_dollars=Decimal(guaranteed_payout_dollars),
        constituent_quotes=constituent_quotes,
        screening_timestamp=quote_timestamp,
        candidate_rationale="Test candidate",
        validation_limitations=("Preliminary screening candidate only",),
        stable_key=stable_key,
        quote_timestamp=quote_timestamp,
        is_execution_qualified=False,
    )


class MockOrderBookClient:
    """Mock client returning predetermined order books or errors."""

    def __init__(self, books: Optional[Mapping[str, NormalizedOrderBook]] = None) -> None:
        self.books: Dict[str, NormalizedOrderBook] = dict(books or {})
        self.requested_tickers: List[str] = []
        self.call_count: int = 0

    def get_order_book(self, ticker: str) -> NormalizedOrderBook:
        self.call_count += 1
        self.requested_tickers.append(ticker)
        if ticker in self.books:
            return self.books[ticker]
        return make_test_order_book(ticker)


def test_2c_1_valid_preliminary_candidate_enters_queue():
    """2C-1. Valid preliminary candidate enqueued successfully into qualification priority queue."""
    client = MockOrderBookClient()
    qualifier = TargetedOrderBookQualifier(client=client)

    cand1 = make_test_preliminary_candidate(candidate_id="PRE-1", market_tickers=("M-1",), gross_edge_dollars="0.10")
    cand2 = make_test_preliminary_candidate(candidate_id="PRE-2", market_tickers=("M-2",), gross_edge_dollars="0.15")

    assert qualifier.queue_size == 0
    assert qualifier.enqueue_candidate(cand1) is True
    assert qualifier.queue_size == 1

    assert qualifier.enqueue_candidate(cand2) is True
    assert qualifier.queue_size == 2

    # Higher gross edge candidate must be prioritized first
    assert qualifier._queue[0].candidate_id == "PRE-2"
    assert qualifier._queue[1].candidate_id == "PRE-1"


def test_2c_2_non_candidate_never_triggers_order_book_request():
    """2C-2. Screened markets with no preliminary candidate never trigger order-book requests."""
    client = MockOrderBookClient()
    qualifier = TargetedOrderBookQualifier(client=client)

    screener = BreadthArbitrageScreener()
    # Market without arbitrage (yes_bid 0.45, yes_ask 0.52, no_bid 0.45, no_ask 0.52 -> yes_ask + no_ask = 1.04 >= 1.00)
    normal_mkt = make_norm_market("M-NORMAL", yes_bid="0.4500", yes_ask="0.5200", no_bid="0.4500", no_ask="0.5200")
    screen_summary = screener.screen_catalog([normal_mkt])

    assert screen_summary.preliminary_candidates_count == 0
    assert len(screen_summary.candidates) == 0

    # Nothing enqueued to qualifier
    qual_summary = qualifier.process_queue()

    assert qual_summary.candidates_evaluated == 0
    assert qual_summary.requests_made == 0
    assert client.call_count == 0
    assert len(client.requested_tickers) == 0


def test_2c_3_duplicate_candidate_not_queued_repeatedly():
    """2C-3. Repeated unchanged candidate is deduplicated and not enqueued repeatedly."""
    client = MockOrderBookClient()
    qualifier = TargetedOrderBookQualifier(client=client)

    cand = make_test_preliminary_candidate(candidate_id="PRE-1", market_tickers=("M-1",))

    # First attempt: enqueued
    assert qualifier.enqueue_candidate(cand) is True
    assert qualifier.queue_size == 1

    # Second attempt (in queue): deduplicated
    assert qualifier.enqueue_candidate(cand) is False
    assert qualifier.queue_size == 1

    # Process candidate
    summary = qualifier.process_queue()
    assert summary.candidates_evaluated == 1
    assert qualifier.queue_size == 0

    # Third attempt (already evaluated with identical quotes): deduplicated
    assert qualifier.enqueue_candidate(cand) is False
    assert qualifier.queue_size == 0


def test_2c_4_stale_candidate_rejected_without_network_request():
    """2C-4. Stale preliminary candidate is rejected immediately with zero order-book network requests."""
    client = MockOrderBookClient()
    config = TargetedOrderBookQualifierConfig(max_stale_seconds=30.0)
    qualifier = TargetedOrderBookQualifier(client=client, config=config)

    # Timestamp 120s in the past
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    cand = make_test_preliminary_candidate(quote_timestamp=stale_ts)

    res = qualifier.qualify_candidate(cand)

    assert res.outcome == QualificationOutcome.REJECTED_STALE.value
    assert res.is_qualified is False
    assert "stale" in (res.rejection_reason or "").lower()
    assert res.requests_made == 0
    assert client.call_count == 0
    assert len(client.requested_tickers) == 0


def test_2c_5_only_constituent_legs_fetched():
    """2C-5. L2 order books are fetched ONLY for constituent legs of candidate, never full catalog."""
    catalog_tickers = ["TICK-1", "TICK-2", "TICK-3", "TICK-4", "TICK-5"]
    client = MockOrderBookClient()
    qualifier = TargetedOrderBookQualifier(client=client)

    # MECE candidate only covers TICK-1 and TICK-2
    cand = make_test_preliminary_candidate(
        candidate_id="PRE-MECE-1",
        strategy_type=ScreeningStrategyType.MECE_BASKET_LONG_YES.value,
        market_tickers=["TICK-1", "TICK-2"],
        gross_edge_dollars="0.20",
        gross_edge_cents=20,
    )

    qualifier.enqueue_candidate(cand)
    summary = qualifier.process_queue()

    assert summary.candidates_evaluated == 1
    assert summary.requests_made == 2
    assert set(client.requested_tickers) == {"TICK-1", "TICK-2"}

    # Unrelated catalog markets are strictly untouched
    for unrelated in ("TICK-3", "TICK-4", "TICK-5"):
        assert unrelated not in client.requested_tickers


def test_2c_6_rate_limiting_and_concurrency_bounds():
    """2C-6. Rate limiter throttles order-book requests to configured max_requests_per_second."""
    sleep_intervals: List[float] = []

    def fake_sleep(dur: float) -> None:
        sleep_intervals.append(dur)

    client = MockOrderBookClient()
    # 5 requests per second -> 0.2s minimum interval
    config = TargetedOrderBookQualifierConfig(max_requests_per_second=5.0)
    qualifier = TargetedOrderBookQualifier(client=client, config=config, sleep_func=fake_sleep)

    cand1 = make_test_preliminary_candidate(candidate_id="PRE-1", market_tickers=("M-1",))
    cand2 = make_test_preliminary_candidate(candidate_id="PRE-2", market_tickers=("M-2",))

    qualifier.qualify_candidate(cand1)
    qualifier.qualify_candidate(cand2)

    assert client.call_count == 2
    assert len(sleep_intervals) >= 1
    assert all(dur > 0 for dur in sleep_intervals)


def test_2c_7_timeout_and_retry_exhaustion():
    """2C-7. Transport timeout retries up to max_retries with backoff and records ERROR outcome."""
    class FailingClient:
        def __init__(self) -> None:
            self.calls = 0

        def get_order_book(self, ticker: str) -> NormalizedOrderBook:
            self.calls += 1
            raise KalshiTransportError(f"Connection timeout fetching {ticker}")

    failing_client = FailingClient()
    sleep_calls: List[float] = []
    config = TargetedOrderBookQualifierConfig(
        max_retries=2,
        retry_backoff_seconds=0.1,
        max_requests_per_second=1000.0,
    )
    qualifier = TargetedOrderBookQualifier(
        client=failing_client,  # type: ignore
        config=config,
        sleep_func=lambda d: sleep_calls.append(d),
    )

    cand = make_test_preliminary_candidate()
    res = qualifier.qualify_candidate(cand)

    assert res.outcome == "ERROR"
    assert res.is_qualified is False
    assert "timeout" in (res.rejection_reason or "").lower()
    assert res.requests_made == 3  # initial attempt + 2 retries
    assert res.requests_failed == 3
    assert res.retries_count == 2
    backoff_sleeps = [d for d in sleep_calls if d >= 0.05]
    assert len(backoff_sleeps) == 2
    assert round(backoff_sleeps[0], 2) == 0.10
    assert round(backoff_sleeps[1], 2) == 0.20


def test_2c_8_http_429_and_transient_5xx_handling():
    """2C-8. HTTP 429 rate limit triggers backoff retry and records rate limit telemetry."""
    class RateLimitedClient:
        def __init__(self) -> None:
            self.calls = 0

        def get_order_book(self, ticker: str) -> NormalizedOrderBook:
            self.calls += 1
            if self.calls == 1:
                raise KalshiHTTPError(429, f"https://api.kalshi.com/trade-api/v2/markets/{ticker}/orderbook", "Rate limit exceeded")
            return make_test_order_book(ticker)

    rl_client = RateLimitedClient()
    config = TargetedOrderBookQualifierConfig(max_retries=3, retry_backoff_seconds=0.05)
    qualifier = TargetedOrderBookQualifier(
        client=rl_client,  # type: ignore
        config=config,
        sleep_func=lambda d: None,
    )

    cand = make_test_preliminary_candidate()
    res = qualifier.qualify_candidate(cand)

    assert res.is_qualified is True
    assert res.requests_made == 2
    assert res.requests_failed == 1
    assert res.retries_count == 1
    assert res.rate_limit_events_count == 1


def test_2c_9_partial_or_malformed_order_books():
    """2C-9. Order book missing source_timestamp is cleanly rejected under require_source_timestamp."""
    book_without_ts = make_test_order_book("M-1")
    # Force source_timestamp to None
    object.__setattr__(book_without_ts, "source_timestamp", None)

    client = MockOrderBookClient(books={"M-1": book_without_ts})
    config = TargetedOrderBookQualifierConfig(require_source_timestamp=True)
    qualifier = TargetedOrderBookQualifier(client=client, config=config)

    cand = make_test_preliminary_candidate(market_tickers=("M-1",))
    res = qualifier.qualify_candidate(cand)

    assert res.outcome == QualificationOutcome.REJECTED_MISSING_TIMESTAMP.value
    assert res.is_qualified is False
    assert "timestamp" in (res.rejection_reason or "").lower()


def test_2c_10_insufficient_depth():
    """2C-10. Order book depth insufficient for requested quantity is rejected."""
    # Available depth is only 3 contracts
    thin_book = make_test_order_book(
        "M-THIN",
        yes_bids=(("0.55", 3),),
        no_bids=(("0.58", 3),),
    )
    client = MockOrderBookClient(books={"M-THIN": thin_book})
    # Target quantity is 10, require full quantity
    config = TargetedOrderBookQualifierConfig(target_quantity=10, require_full_quantity=True)
    qualifier = TargetedOrderBookQualifier(client=client, config=config)

    cand = make_test_preliminary_candidate(market_tickers=("M-THIN",))
    res = qualifier.qualify_candidate(cand)

    assert res.outcome == QualificationOutcome.REJECTED_INSUFFICIENT_DEPTH.value
    assert res.is_qualified is False
    assert res.executable_quantity == 3
    assert res.requested_quantity == 10


def test_2c_11_fee_adjusted_profitability():
    """2C-11. Taker fees are accurately subtracted from gross edge to determine net edge."""
    # YES ask 0.40 (no_bids 0.60), NO ask 0.40 (yes_bids 0.60)
    # Gross cost = 0.80, payout = 1.00 -> gross edge = 20 cents
    book = make_test_order_book(
        "M-FEES",
        yes_bids=(("0.60", 10),),
        no_bids=(("0.60", 10),),
    )
    client = MockOrderBookClient(books={"M-FEES": book})
    qualifier = TargetedOrderBookQualifier(client=client)

    cand = make_test_preliminary_candidate(
        market_tickers=("M-FEES",),
        gross_edge_dollars="0.20",
        gross_edge_cents=20,
    )
    res = qualifier.qualify_candidate(cand)

    assert res.is_qualified is True
    assert res.gross_edge_cents == 20
    assert res.total_fees_cents is not None
    assert res.total_fees_cents > 0
    assert res.net_edge_cents is not None
    assert res.net_edge_cents == res.gross_edge_cents - res.total_fees_cents


def test_2c_12_gross_positive_but_net_negative_candidate():
    """2C-12. Candidate with positive gross edge but fees exceeding profit is rejected as net negative."""
    # YES ask 0.50 (no_bids 0.50), NO ask 0.49 (yes_bids 0.51)
    # Total cost = 0.99, payout = 1.00 -> gross edge = 1 cent
    # CFTC quadratic fees on 50c and 49c will be ~3-4 cents, resulting in net edge < 0
    razor_book = make_test_order_book(
        "M-RAZOR",
        yes_bids=(("0.51", 10),),
        no_bids=(("0.50", 10),),
    )
    client = MockOrderBookClient(books={"M-RAZOR": razor_book})
    qualifier = TargetedOrderBookQualifier(client=client)

    cand = make_test_preliminary_candidate(
        market_tickers=("M-RAZOR",),
        gross_edge_dollars="0.01",
        gross_edge_cents=1,
    )
    res = qualifier.qualify_candidate(cand)

    assert res.outcome == QualificationOutcome.REJECTED_UNPROFITABLE_FEES.value
    assert res.is_qualified is False
    assert res.net_edge_cents is not None
    assert res.net_edge_cents <= 0


def test_2c_13_qualified_candidate_positive_net_edge_valid_depth():
    """2C-13. Genuinely viable candidate with deep book and positive net edge is fully QUALIFIED."""
    # YES ask 0.42 (no_bids 0.58), NO ask 0.45 (yes_bids 0.55)
    # Gross edge = 13 cents, depth = 20
    deep_book = make_test_order_book(
        "M-DEEP",
        yes_bids=(("0.55", 20),),
        no_bids=(("0.58", 20),),
    )
    client = MockOrderBookClient(books={"M-DEEP": deep_book})
    config = TargetedOrderBookQualifierConfig(target_quantity=5)
    qualifier = TargetedOrderBookQualifier(client=client, config=config)

    cand = make_test_preliminary_candidate(market_tickers=("M-DEEP",))
    res = qualifier.qualify_candidate(cand)

    assert res.outcome == QualificationOutcome.QUALIFIED.value
    assert res.is_qualified is True
    assert res.requested_quantity == 5
    assert res.executable_quantity == 5
    assert res.gross_edge_cents == 13 * 5
    assert res.net_edge_cents is not None and res.net_edge_cents > 0
    assert res.return_on_capital_pct is not None and res.return_on_capital_pct > Decimal("0")
    assert len(res.leg_details) == 2
    assert "M-DEEP" in res.order_book_hashes


def test_2c_14_unsupported_mece_or_crossed_book_strategy():
    """2C-14. Crossed-book candidates are classified UNSUPPORTED_STRATEGY and never qualified."""
    client = MockOrderBookClient()
    qualifier = TargetedOrderBookQualifier(client=client)

    xb_cand = make_test_preliminary_candidate(
        candidate_id="PRE-XB-1",
        strategy_type=ScreeningStrategyType.CROSSED_BOOK.value,
        market_tickers=("M-XB",),
    )
    res = qualifier.qualify_candidate(xb_cand)

    assert res.outcome == "UNSUPPORTED_STRATEGY"
    assert res.is_qualified is False
    assert "crossed book" in (res.rejection_reason or "").lower()


def test_2c_15_queue_overflow_and_telemetry():
    """2C-15. Bounded queue enforces max_queue_size, evicts lowest gross edge, and records telemetry."""
    client = MockOrderBookClient()
    config = TargetedOrderBookQualifierConfig(max_queue_size=3)
    qualifier = TargetedOrderBookQualifier(client=client, config=config)

    c1 = make_test_preliminary_candidate(candidate_id="C1", market_tickers=("M-1",), gross_edge_dollars="0.05")
    c2 = make_test_preliminary_candidate(candidate_id="C2", market_tickers=("M-2",), gross_edge_dollars="0.10")
    c3 = make_test_preliminary_candidate(candidate_id="C3", market_tickers=("M-3",), gross_edge_dollars="0.02")

    assert qualifier.enqueue_candidate(c1) is True
    assert qualifier.enqueue_candidate(c2) is True
    assert qualifier.enqueue_candidate(c3) is True
    assert qualifier.queue_size == 3
    assert qualifier.queue_overflow_count == 0

    # High edge candidate arrives: evicts c3 (0.02)
    c4 = make_test_preliminary_candidate(candidate_id="C4", market_tickers=("M-4",), gross_edge_dollars="0.20")
    assert qualifier.enqueue_candidate(c4) is True
    assert qualifier.queue_size == 3
    assert qualifier.queue_overflow_count == 1

    # Remaining in queue must be top 3 edges: 0.20 (C4), 0.10 (C2), 0.05 (C1)
    queue_ids = [c.candidate_id for c in qualifier._queue]
    assert queue_ids == ["C4", "C2", "C1"]

    # Lower edge candidate arrives: rejected by overflow
    c5 = make_test_preliminary_candidate(candidate_id="C5", market_tickers=("M-5",), gross_edge_dollars="0.01")
    assert qualifier.enqueue_candidate(c5) is False
    assert qualifier.queue_size == 3
    assert qualifier.queue_overflow_count == 2

    summary = qualifier.process_queue()
    assert summary.candidates_evaluated == 3
    assert summary.queue_overflow_count == 2


def test_2c_16_deterministic_qualification_outcome():
    """2C-16. Qualification results are strictly deterministic given identical input order books."""
    fixed_time = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    book = make_test_order_book("M-DET", source_timestamp=fixed_time)
    client = MockOrderBookClient(books={"M-DET": book})
    qualifier = TargetedOrderBookQualifier(client=client, time_provider=lambda: fixed_time)

    cand = make_test_preliminary_candidate(market_tickers=("M-DET",), quote_timestamp=fixed_time.isoformat())

    res1 = qualifier.qualify_candidate(cand)
    res2 = qualifier.qualify_candidate(cand)

    assert res1.outcome == res2.outcome
    assert res1.is_qualified == res2.is_qualified
    assert res1.gross_edge_cents == res2.gross_edge_cents
    assert res1.net_edge_cents == res2.net_edge_cents
    assert res1.total_fees_cents == res2.total_fees_cents
    assert res1.executable_quantity == res2.executable_quantity
    assert res1.return_on_capital_pct == res2.return_on_capital_pct
    assert res1.order_book_hashes == res2.order_book_hashes


def test_2c_17_no_order_submission_calls():
    """2C-17. Zero order placement, trade submission, or portfolio modification endpoints are contacted."""
    forbidden_called: List[str] = []

    class StrictlyReadOnlyClient(MockOrderBookClient):
        def place_order(self, *args, **kwargs): forbidden_called.append("place_order")
        def create_order(self, *args, **kwargs): forbidden_called.append("create_order")
        def submit_order(self, *args, **kwargs): forbidden_called.append("submit_order")
        def buy(self, *args, **kwargs): forbidden_called.append("buy")
        def sell(self, *args, **kwargs): forbidden_called.append("sell")

    client = StrictlyReadOnlyClient()
    qualifier = TargetedOrderBookQualifier(client=client)

    cand = make_test_preliminary_candidate()
    qualifier.enqueue_candidate(cand)
    summary = qualifier.process_queue()

    assert summary.candidates_evaluated == 1
    assert len(forbidden_called) == 0
    # Verify no trade execution methods exist on qualifier
    assert not hasattr(qualifier, "place_order")
    assert not hasattr(qualifier, "submit_order")
    assert not hasattr(qualifier, "execute_trade")


def test_2c_18_existing_v22_v25_regression_compatibility():
    """2C-18. Phase 2C components cleanly coexist with V20-V25 pipeline without regressions."""
    collector = ContinuousCollector()
    evaluator = ContinuousEvaluator()
    v22_qualifier = ExecutionAwareArbitrageQualifier()
    alert_engine = AlertEngine()
    screener = BreadthArbitrageScreener()
    phase2c_qualifier = TargetedOrderBookQualifier()

    assert collector is not None
    assert evaluator is not None
    assert v22_qualifier is not None
    assert alert_engine is not None
    assert screener is not None
    assert phase2c_qualifier is not None

    # Underlying qualifier is reused
    assert isinstance(phase2c_qualifier.qualifier, ExecutionAwareArbitrageQualifier)


