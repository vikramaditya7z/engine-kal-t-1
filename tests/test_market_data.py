from datetime import datetime, timezone
from decimal import Decimal

import pytest

from kalshi_arbitrage import (
    MarketDataInputError,
    NormalizedOrderBook,
    OrderBookLevel,
    normalize_event,
    normalize_market,
    normalize_order_book,
    parse_price_dollars,
    parse_quantity,
)


def market_payload():
    return {
        "market": {
            "ticker": "KXTEST-YES",
            "event_ticker": "KXTEST-EVENT",
            "market_type": "binary",
            "status": "open-but-new",
            "title": "Will the test condition occur?",
            "subtitle": "A normalized test market",
            "yes_sub_title": "The condition occurs",
            "no_sub_title": "The condition does not occur",
            "rules_primary": "Settlement uses the published source.",
            "rules_secondary": "Additional settlement details.",
            "yes_bid_dollars": "0.1234",
            "yes_ask_dollars": "0.1300",
            "no_bid_dollars": "0.8700",
            "no_ask_dollars": "0.8766",
            "last_price_dollars": "0.1250",
            "yes_bid_size_fp": "12.50",
            "yes_ask_size_fp": "3.25",
            "volume_fp": "100.50",
            "volume_24h_fp": "20.25",
            "open_interest_fp": "50.75",
            "created_time": "2026-01-01T00:00:00Z",
            "updated_time": None,
            "price_ranges": [
                {"start": "0.0000", "end": "1.0000", "step": "0.0001"}
            ],
            "is_provisional": False,
        }
    }


def event_payload():
    return {
        "event": {
            "event_ticker": "KXTEST-EVENT",
            "series_ticker": "KXTEST",
            "title": "Test event",
            "sub_title": "Possible test outcomes",
            "mutually_exclusive": True,
            "settlement_sources": [{"name": "Official source", "url": "https://example.test"}],
            "markets": [
                {"ticker": "KXTEST-YES", "event_ticker": "KXTEST-EVENT"},
                {"ticker": "KXTEST-NO", "event_ticker": "KXTEST-EVENT"},
            ],
        }
    }


def test_exact_numeric_parsing_preserves_subcent_and_fractional_values():
    assert parse_price_dollars("0.1234") == Decimal("0.1234")
    assert parse_quantity("12.50") == Decimal("12.50")

    market = normalize_market(market_payload())
    assert market.yes_bid_dollars == Decimal("0.1234")
    assert market.yes_bid_size == Decimal("12.50")
    assert market.created_time == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert market.updated_time is None


def test_timestamp_parser_accepts_observed_five_digit_fractional_seconds():
    payload = market_payload()
    payload["market"]["updated_time"] = "2026-10-09T10:28:39.57017+00:00"

    market = normalize_market(payload)

    assert market.updated_time == datetime(
        2026, 10, 9, 10, 28, 39, 570170, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-09T10:28:39.57017",
        "2026-10-09T10:28:39.1234567+00:00",
        "not-a-timestamp",
    ],
)
def test_timestamp_parser_rejects_naive_overprecision_and_malformed_values(timestamp):
    payload = market_payload()
    payload["market"]["updated_time"] = timestamp

    with pytest.raises(MarketDataInputError, match="updated_time"):
        normalize_market(payload)


def test_valid_event_preserves_mutual_exclusion_without_claiming_exhaustiveness():
    event = normalize_event(event_payload())
    assert event.event_ticker == "KXTEST-EVENT"
    assert event.mutually_exclusive is True
    assert event.market_tickers == ("KXTEST-YES", "KXTEST-NO")
    assert event.settlement_sources[0].url == "https://example.test"
    assert not hasattr(event, "collectively_exhaustive")


def test_valid_order_book_has_exact_sorted_levels():
    order_book = normalize_order_book(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.1234", "1.50"], ["0.2500", "2.00"]],
                "no_dollars": [["0.7000", "3.25"]],
            }
        },
        "KXTEST-YES",
    )
    assert order_book.yes_bids[0].price_dollars == Decimal("0.1234")
    assert order_book.yes_bids[0].quantity == Decimal("1.50")
    assert order_book.no_bids[0].quantity == Decimal("3.25")


@pytest.mark.parametrize("value", [True, False, 0.25, float("nan"), float("inf"), "NaN", "Infinity", "bad"])
def test_numeric_parsers_reject_boolean_float_nonfinite_and_invalid_values(value):
    with pytest.raises(MarketDataInputError):
        parse_price_dollars(value)
    with pytest.raises(MarketDataInputError):
        parse_quantity(value)


@pytest.mark.parametrize("value", ["-0.01", "1.0001", "-1", "2"])
def test_price_range_is_enforced(value):
    with pytest.raises(MarketDataInputError):
        parse_price_dollars(value)


@pytest.mark.parametrize("value", ["-0.01", "-1"])
def test_quantity_must_be_non_negative(value):
    with pytest.raises(MarketDataInputError):
        parse_quantity(value)


def test_order_book_quantity_must_be_positive():
    with pytest.raises(MarketDataInputError):
        normalize_order_book(
            {"orderbook_fp": {"yes_dollars": [["0.10", "0"]], "no_dollars": []}},
            "KXTEST-YES",
        )


@pytest.mark.parametrize("field", ["ticker", "event_ticker", "market_type", "status", "title"])
def test_required_market_fields_are_rejected_when_missing(field):
    payload = market_payload()
    del payload["market"][field]
    with pytest.raises(MarketDataInputError):
        normalize_market(payload)


def test_unsupported_market_type_is_rejected():
    payload = market_payload()
    payload["market"]["market_type"] = "multivariate"
    with pytest.raises(MarketDataInputError):
        normalize_market(payload)


def test_unknown_status_is_preserved_as_raw_data():
    market = normalize_market(market_payload())
    assert market.status == "open-but-new"


def test_malformed_optional_field_is_rejected_but_null_is_allowed():
    payload = market_payload()
    payload["market"]["yes_ask_dollars"] = None
    assert normalize_market(payload).yes_ask_dollars is None

    payload = market_payload()
    payload["market"]["yes_ask_dollars"] = "not-a-price"
    with pytest.raises(MarketDataInputError):
        normalize_market(payload)


def test_empty_rules_primary_from_api_is_treated_as_unspecified():
    payload = market_payload()
    payload["market"]["rules_primary"] = ""
    market = normalize_market(payload)
    assert market.rules_primary is None

    payload["market"]["rules_primary"] = 123
    with pytest.raises(MarketDataInputError):
        normalize_market(payload)


def test_empty_expiration_value_from_api_is_treated_as_unspecified():
    payload = market_payload()
    payload["market"]["expiration_value"] = ""
    market = normalize_market(payload)
    assert market.expiration_value is None

    payload["market"]["expiration_value"] = 123
    with pytest.raises(MarketDataInputError):
        normalize_market(payload)


def test_empty_result_from_unsettled_api_market_is_treated_as_unspecified():
    payload = market_payload()
    payload["market"]["result"] = ""
    market = normalize_market(payload)
    assert market.result is None

    payload["market"]["result"] = 123
    with pytest.raises(MarketDataInputError):
        normalize_market(payload)


def test_malformed_collections_and_identifiers_are_rejected():
    with pytest.raises(MarketDataInputError):
        normalize_market(None)
    with pytest.raises(MarketDataInputError):
        normalize_order_book({"orderbook_fp": None}, "KXTEST-YES")

    payload = event_payload()
    payload["event"]["event_ticker"] = ""
    with pytest.raises(MarketDataInputError):
        normalize_event(payload)


def test_event_relationship_and_market_mapping_are_not_inferred():
    event = normalize_event(event_payload())
    market = normalize_market(market_payload())
    assert event.event_ticker == market.event_ticker
    assert not hasattr(market, "yes_outcome")


def test_order_book_levels_must_be_ascending():
    with pytest.raises(MarketDataInputError):
        normalize_order_book(
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.20", "1"], ["0.10", "1"]],
                    "no_dollars": [],
                }
            },
            "KXTEST-YES",
        )


def test_order_book_source_timestamp_validation():
    payload = {
        "orderbook_fp": {
            "yes_dollars": [["0.10", "1"]],
            "no_dollars": [["0.20", "1"]],
        }
    }
    valid_ts = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)

    # Valid timezone-aware timestamp
    book = normalize_order_book(payload, "KXTEST-YES", source_timestamp=valid_ts)
    assert book.source_timestamp == valid_ts

    # None is permitted
    book_none = normalize_order_book(payload, "KXTEST-YES", source_timestamp=None)
    assert book_none.source_timestamp is None

    # Naive datetime must be rejected
    naive_ts = datetime(2026, 10, 9, 12, 0, 0)
    with pytest.raises(MarketDataInputError, match="must include a timezone"):
        normalize_order_book(payload, "KXTEST-YES", source_timestamp=naive_ts)

    # Non-datetime object must be rejected
    with pytest.raises(MarketDataInputError, match="must be a datetime or null"):
        normalize_order_book(payload, "KXTEST-YES", source_timestamp="2026-10-09T12:00:00Z")

    # Direct NormalizedOrderBook instantiation validation
    with pytest.raises(MarketDataInputError, match="must include a timezone"):
        NormalizedOrderBook(
            market_ticker="KXTEST-YES",
            yes_bids=(OrderBookLevel(price_dollars=Decimal("0.10"), quantity=Decimal("1")),),
            no_bids=(OrderBookLevel(price_dollars=Decimal("0.20"), quantity=Decimal("1")),),
            source_timestamp=naive_ts,
        )
