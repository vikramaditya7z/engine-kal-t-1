"""Small, read-only REST client for public Kalshi market data."""

from dataclasses import dataclass
import json
from math import isfinite
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, quote
from urllib.request import Request, urlopen

from .market_data import (
    MarketDataInputError,
    NormalizedEvent,
    NormalizedMarket,
    NormalizedOrderBook,
    normalize_event,
    normalize_market,
    normalize_order_book,
)


PRODUCTION_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MARKET_PAGE_SIZE = 100
MAX_MARKET_PAGE_SIZE = 1000


class KalshiClientError(Exception):
    """Base class for REST transport, HTTP, and response errors."""


class KalshiTransportError(KalshiClientError):
    """The request could not be completed or the connection failed."""


class KalshiHTTPError(KalshiClientError):
    """Kalshi returned a non-success HTTP status."""

    def __init__(self, status_code: int, url: str, body: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"Kalshi returned HTTP {status_code} for {url}")


class KalshiJSONError(KalshiClientError):
    """A successful response did not contain valid JSON."""


class KalshiResponseError(KalshiClientError):
    """A successful JSON response did not match the documented envelope."""


class KalshiPaginationError(KalshiClientError):
    """Pagination cannot continue safely, such as when a cursor repeats."""


@dataclass(frozen=True)
class HTTPResponse:
    """Minimal response value used by the standard-library transport and tests."""

    status_code: int
    body: bytes


class UrllibGetTransport:
    """GET-only standard-library transport."""

    def get(
        self, url: str, params: Mapping[str, str], timeout_seconds: float
    ) -> HTTPResponse:
        query = urlencode(params)
        request_url = f"{url}?{query}" if query else url
        request = Request(request_url, method="GET", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return HTTPResponse(response.status, response.read())
        except HTTPError as exc:
            body = exc.read()
            return HTTPResponse(exc.code, body)
        except (URLError, OSError, TimeoutError) as exc:
            raise KalshiTransportError(f"GET request failed for {request_url}") from exc


Transport = Callable[[str, Mapping[str, str], float], HTTPResponse]


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise KalshiResponseError(f"{name} must be an object")
    return value


class KalshiRestClient:
    """Read-only client for normalized market and event data."""

    def __init__(
        self,
        environment: str = "production",
        base_url: Optional[str] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Optional[Transport] = None,
    ) -> None:
        if environment not in ("production", "demo"):
            raise ValueError("environment must be 'production' or 'demo'")
        if base_url is not None and (not isinstance(base_url, str) or not base_url):
            raise ValueError("base_url must be a non-empty string")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be a positive number")
        if timeout_seconds <= 0 or not isfinite(float(timeout_seconds)):
            raise ValueError("timeout_seconds must be a positive number")
        if transport is not None and not callable(transport) and not hasattr(transport, "get"):
            raise ValueError("transport must provide a get method")

        default_url = DEMO_BASE_URL if environment == "demo" else PRODUCTION_BASE_URL
        self.base_url = (base_url or default_url).rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self._transport = transport or UrllibGetTransport()

    def _get_json(self, path: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        url = urljoin(f"{self.base_url}/", path.lstrip("/"))
        try:
            response = self._transport.get(url, params, self.timeout_seconds)
        except KalshiClientError:
            raise
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise KalshiTransportError(f"GET request failed for {url}") from exc
        except Exception as exc:
            raise KalshiTransportError(f"GET request failed for {url}") from exc

        if not isinstance(response, HTTPResponse):
            raise KalshiResponseError("transport must return HTTPResponse")
        if isinstance(response.status_code, bool) or not isinstance(response.status_code, int):
            raise KalshiResponseError("transport response status_code must be an integer")
        if not isinstance(response.body, bytes):
            raise KalshiResponseError("transport response body must be bytes")
        if response.status_code < 200 or response.status_code >= 300:
            body = response.body.decode("utf-8", errors="replace")
            raise KalshiHTTPError(response.status_code, url, body)
        try:
            decoded = response.body.decode("utf-8")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KalshiJSONError(f"invalid JSON response from {url}") from exc
        return _object(payload, "response")

    def list_markets(
        self,
        *,
        limit: int = DEFAULT_MARKET_PAGE_SIZE,
        cursor: Optional[str] = None,
        event_ticker: Optional[str] = None,
        series_ticker: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Tuple[NormalizedMarket, ...]:
        """Fetch and normalize all market pages from the requested starting cursor."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if not 0 <= limit <= MAX_MARKET_PAGE_SIZE:
            raise ValueError(f"limit must be between 0 and {MAX_MARKET_PAGE_SIZE}")
        if cursor is not None:
            _identifier(cursor, "cursor")
        filters = (("event_ticker", event_ticker), ("series_ticker", series_ticker), ("status", status))
        for name, value in filters:
            if value is not None:
                _identifier(value, name)

        markets = []
        seen_cursors = set()
        next_cursor = cursor
        while True:
            params: Dict[str, str] = {"limit": str(limit)}
            for name, value in filters:
                if value is not None:
                    params[name] = value
            if next_cursor:
                params["cursor"] = next_cursor
            payload = self._get_json("/markets", params)
            raw_markets = payload.get("markets")
            if not isinstance(raw_markets, list):
                raise KalshiResponseError("response markets must be an array")
            for raw_market in raw_markets:
                try:
                    markets.append(normalize_market(_object(raw_market, "market")))
                except MarketDataInputError:
                    raise
            returned_cursor = payload.get("cursor")
            if returned_cursor in (None, ""):
                break
            if not isinstance(returned_cursor, str):
                raise KalshiResponseError("response cursor must be a string or null")
            if returned_cursor in seen_cursors or returned_cursor == next_cursor:
                raise KalshiPaginationError("market pagination cursor repeated")
            seen_cursors.add(returned_cursor)
            next_cursor = returned_cursor
        return tuple(markets)

    def get_market(self, ticker: str) -> NormalizedMarket:
        ticker = _identifier(ticker, "ticker")
        payload = self._get_json(f"/markets/{quote(ticker, safe='')}", {})
        market = _object(payload.get("market"), "response market")
        return normalize_market(market)

    def get_event(
        self, event_ticker: str, *, with_nested_markets: bool = False
    ) -> NormalizedEvent:
        event_ticker = _identifier(event_ticker, "event_ticker")
        if not isinstance(with_nested_markets, bool):
            raise ValueError("with_nested_markets must be a boolean")
        params = {"with_nested_markets": "true"} if with_nested_markets else {}
        payload = self._get_json(f"/events/{quote(event_ticker, safe='')}", params)
        event_payload = _object(payload.get("event"), "response event")
        nested_markets = event_payload.get("markets")
        if nested_markets is None:
            nested_markets = payload.get("markets")
        if nested_markets is not None:
            if not isinstance(nested_markets, list):
                raise KalshiResponseError("event markets must be an array")
            for nested_market in nested_markets:
                normalize_market(_object(nested_market, "event market"))
        return normalize_event(payload)

    def get_order_book(self, ticker: str) -> NormalizedOrderBook:
        ticker = _identifier(ticker, "ticker")
        payload = self._get_json(f"/markets/{quote(ticker, safe='')}/orderbook", {})
        return normalize_order_book(payload, market_ticker=ticker)
