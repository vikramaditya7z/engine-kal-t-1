from datetime import datetime, timezone
import json

import pytest

from kalshi_arbitrage import (
    DEMO_BASE_URL,
    HTTPResponse,
    KalshiHTTPError,
    KalshiJSONError,
    KalshiPaginationError,
    KalshiResponseError,
    KalshiRestClient,
    KalshiTransportError,
    MarketDataInputError,
    PRODUCTION_BASE_URL,
    format_http_date,
    parse_http_date,
)


def market(ticker="KX-1"):
    return {
        "ticker": ticker,
        "event_ticker": "KX-EVENT",
        "market_type": "binary",
        "status": "open",
        "title": "Test market",
        "yes_bid_dollars": "0.4000",
        "yes_ask_dollars": "0.4100",
        "no_bid_dollars": "0.5900",
        "no_ask_dollars": "0.6000",
        "yes_bid_size_fp": "1.50",
        "yes_ask_size_fp": "2.00",
        "volume_fp": "10.00",
        "volume_24h_fp": "2.00",
        "open_interest_fp": "5.00",
        "created_time": "2026-01-01T00:00:00Z",
        "updated_time": "2026-01-01T00:00:00Z",
        "open_time": "2026-01-01T00:00:00Z",
        "close_time": "2026-01-02T00:00:00Z",
        "latest_expiration_time": "2026-01-02T00:00:00Z",
        "expiration_time": "2026-01-02T00:00:00Z",
        "rules_primary": "Documented settlement rules.",
        "rules_secondary": "Additional details.",
        "expiration_value": "value",
        "price_ranges": [{"start": "0.00", "end": "1.00", "step": "0.01"}],
        "is_provisional": False,
    }


def event():
    return {
        "event_ticker": "KX-EVENT",
        "series_ticker": "KX",
        "title": "Test event",
        "sub_title": "Possible outcomes",
        "category": "test",
        "mutually_exclusive": True,
        "collateral_return_type": "MECNET",
        "markets": [market("KX-1"), market("KX-2")],
    }


class FakeTransport:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def get(self, url, params, timeout_seconds):
        self.calls.append((url, dict(params), timeout_seconds))
        if self.error:
            raise self.error
        return self.responses.pop(0)


def response(payload, status_code=200, headers=None):
    return HTTPResponse(status_code, json.dumps(payload).encode("utf-8"), headers=headers or {})


def client(fake):
    return KalshiRestClient(base_url="https://example.test/trade-api/v2", transport=fake)


def test_defaults_and_demo_environment_are_explicit():
    assert KalshiRestClient().base_url == PRODUCTION_BASE_URL
    assert KalshiRestClient(environment="demo").base_url == DEMO_BASE_URL


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_timeout_must_be_finite_and_positive(timeout):
    with pytest.raises(ValueError):
        KalshiRestClient(timeout_seconds=timeout)


def test_list_markets_builds_query_and_normalizes_market():
    fake = FakeTransport([response({"markets": [market()], "cursor": ""})])
    result = client(fake).list_markets(limit=25, event_ticker="KX-EVENT", status="open")
    assert result[0].ticker == "KX-1"
    assert fake.calls == [
        (
            "https://example.test/trade-api/v2/markets",
            {"limit": "25", "event_ticker": "KX-EVENT", "status": "open"},
            10.0,
        )
    ]


def test_list_markets_follows_cursor_until_empty():
    fake = FakeTransport(
        [
            response({"markets": [market("KX-1")], "cursor": "next"}),
            response({"markets": [market("KX-2")], "cursor": None}),
        ]
    )
    result = client(fake).list_markets(limit=100)
    assert [item.ticker for item in result] == ["KX-1", "KX-2"]
    assert fake.calls[1][1]["cursor"] == "next"


def test_list_markets_max_markets_bounds_pagination():
    fake = FakeTransport(
        [
            response({"markets": [market("KX-1"), market("KX-2")], "cursor": "next"}),
            response({"markets": [market("KX-3"), market("KX-4")], "cursor": "next2"}),
        ]
    )
    # Asking for max_markets=3 should fetch page 1 and page 2, returning exactly 3 items
    result = client(fake).list_markets(limit=2, max_markets=3)
    assert len(result) == 3
    assert [item.ticker for item in result] == ["KX-1", "KX-2", "KX-3"]
    assert len(fake.calls) == 2
    # Verify effective limit on page 2 was clamped to remaining needed (1)
    assert fake.calls[1][1]["limit"] == "1"



def test_repeated_cursor_is_rejected():
    fake = FakeTransport([response({"markets": [], "cursor": "same"})])
    with pytest.raises(KalshiPaginationError):
        client(fake).list_markets(cursor="same")


def test_get_market_uses_escaped_ticker_and_normalizes_envelope():
    fake = FakeTransport([response({"market": market("KX/1")})])
    result = client(fake).get_market("KX/1")
    assert result.ticker == "KX/1"
    assert fake.calls[0][0].endswith("/markets/KX%2F1")
    assert fake.calls[0][1] == {}


def test_get_event_can_request_nested_markets_and_normalizes_event():
    fake = FakeTransport([response({"event": event()})])
    result = client(fake).get_event("KX-EVENT", with_nested_markets=True)
    assert result.event_ticker == "KX-EVENT"
    assert result.market_tickers == ("KX-1", "KX-2")
    assert fake.calls[0][1] == {"with_nested_markets": "true"}


def test_get_event_rejects_malformed_nested_market_data():
    malformed = event()
    malformed["markets"][0]["yes_bid_dollars"] = "not-a-price"
    fake = FakeTransport([response({"event": malformed})])
    with pytest.raises(ValueError):
        client(fake).get_event("KX-EVENT", with_nested_markets=True)


@pytest.mark.parametrize("status", [401, 404, 429, 500, 503])
def test_http_errors_are_explicit(status):
    fake = FakeTransport([response({"error": "failure"}, status)])
    with pytest.raises(KalshiHTTPError) as exc_info:
        client(fake).get_market("KX-1")
    assert exc_info.value.status_code == status


def test_connection_failure_is_explicit():
    fake = FakeTransport(error=TimeoutError("timed out"))
    with pytest.raises(KalshiTransportError):
        client(fake).get_market("KX-1")


def test_invalid_json_is_explicit():
    fake = FakeTransport([HTTPResponse(200, b"not json")])
    with pytest.raises(KalshiJSONError):
        client(fake).get_market("KX-1")


@pytest.mark.parametrize(
    "payload",
    [{}, {"market": None}, {"markets": {}}, {"markets": [{"ticker": "KX-1"}]}],
)
def test_malformed_response_envelopes_are_rejected(payload):
    fake = FakeTransport([response(payload)])
    with pytest.raises((KalshiResponseError, ValueError)):
        client(fake).get_market("KX-1")


@pytest.mark.parametrize(
    "method, argument",
    [("get_market", ""), ("get_event", None), ("get_order_book", "")],
)
def test_identifiers_are_validated(method, argument):
    fake = FakeTransport()
    with pytest.raises(ValueError):
        getattr(client(fake), method)(argument)


def test_get_order_book_normalizes_envelope():
    raw_orderbook = {
        "orderbook_fp": {
            "yes_dollars": [["0.4000", "10.00"]],
            "no_dollars": [["0.5500", "5.00"]],
        }
    }
    fake = FakeTransport([response(raw_orderbook)])
    result = client(fake).get_order_book("KX-1")
    assert result.market_ticker == "KX-1"
    assert len(result.yes_bids) == 1
    assert len(result.no_bids) == 1
    assert fake.calls[0][0].endswith("/markets/KX-1/orderbook")
    assert fake.calls[0][1] == {}


def test_client_transport_is_get_only():
    fake = FakeTransport([response({"market": market()})])
    client(fake).get_market("KX-1")
    assert len(fake.calls) == 1
    assert not hasattr(fake, "post")


def test_parse_and_format_http_date():
    dt = datetime(2026, 10, 9, 15, 0, 0, tzinfo=timezone.utc)
    formatted = format_http_date(dt)
    assert "Oct 2026" in formatted
    assert "GMT" in formatted

    parsed = parse_http_date(formatted)
    assert parsed == dt
    assert parsed.tzinfo == timezone.utc

    assert parse_http_date(None) is None
    assert parse_http_date("") is None
    assert parse_http_date("   ") is None

    with pytest.raises(ValueError, match="malformed HTTP Date header"):
        parse_http_date("not-a-date")


def test_http_response_headers_case_insensitivity():
    resp = HTTPResponse(
        200,
        b"{}",
        headers={"Date": "Fri, 09 Oct 2026 15:00:00 GMT", "Content-Type": "application/json"},
    )
    assert resp.get_header("date") == "Fri, 09 Oct 2026 15:00:00 GMT"
    assert resp.get_header("DATE") == "Fri, 09 Oct 2026 15:00:00 GMT"
    assert resp.get_header("content-type") == "application/json"
    assert resp.get_header("missing") is None
    assert resp.get_header("missing", "default_val") == "default_val"


def test_get_order_book_captures_http_date_header():
    raw_orderbook = {
        "orderbook_fp": {
            "yes_dollars": [["0.4000", "10.00"]],
            "no_dollars": [["0.5500", "5.00"]],
        }
    }
    date_str = "Fri, 09 Oct 2026 15:00:00 GMT"
    fake = FakeTransport([response(raw_orderbook, headers={"Date": date_str})])
    result = client(fake).get_order_book("KX-1")
    assert result.market_ticker == "KX-1"
    assert result.source_timestamp == datetime(2026, 10, 9, 15, 0, 0, tzinfo=timezone.utc)


def test_get_order_book_rejects_malformed_http_date_header():
    raw_orderbook = {
        "orderbook_fp": {
            "yes_dollars": [["0.4000", "10.00"]],
            "no_dollars": [["0.5500", "5.00"]],
        }
    }
    fake = FakeTransport([response(raw_orderbook, headers={"Date": "not-a-valid-date"})])
    with pytest.raises(MarketDataInputError, match="malformed HTTP Date header"):
        client(fake).get_order_book("KX-1")


def test_get_order_book_missing_date_header():
    raw_orderbook = {
        "orderbook_fp": {
            "yes_dollars": [["0.4000", "10.00"]],
            "no_dollars": [["0.5500", "5.00"]],
        }
    }
    fake = FakeTransport([response(raw_orderbook, headers={})])
    result = client(fake).get_order_book("KX-1")
    assert result.source_timestamp is None
