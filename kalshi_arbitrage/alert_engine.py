"""Qualified-opportunity alert and notification engine for Kalshi prediction markets.

V24 Alerts & Long-Running Reliability:
Consumes candidate lifecycle and V22 execution qualification events to provide reliable,
deduplicated alerts across multiple channels (local history, desktop, webhooks) with
graceful failure isolation, material-change filtering, re-alert cooldowns, and secret redaction.

Core Invariants:
1. Qualified-Only Opportunity Alerts: Only current, valid, execution-qualified candidates
   trigger actionable opportunity alerts. Theoretical candidates, unqualified candidates,
   historical replays, and simulation fixtures never trigger alerts.
2. Persistent Deduplication: State persists across restarts so that daemon restarts do not
   trigger duplicate notification storms.
3. Decoupled Asynchronous Dispatch: Alert delivery is completely asynchronous via bounded
   queues; slow or failing notification endpoints never block market data collection or evaluation.
4. Fail-Closed Error Isolation: Delivery failures and retries are recorded without crashing
   the scanning loops or masking failures as successes.
5. Strict Secret Redaction: API keys, webhook tokens, and sensitive headers are never logged
   or exposed in status endpoints.
"""

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)
import urllib.error
import urllib.parse
import urllib.request
import uuid

logger = logging.getLogger("kalshi_arbitrage.alert_engine")

DEFAULT_ALERT_DISCLAIMER: str = (
    "DISCLAIMER: Static snapshot qualification only. Available order-book depth and fees "
    "evaluated against resting limit orders at observation time. Does not guarantee live fills, "
    "queue priority, or atomic multi-leg execution. Subject to Kalshi CFTC taker fee schedule."
)


# -----------------------------------------------------------------------------
# Secret and URL Sanitization Utilities
# -----------------------------------------------------------------------------

def sanitize_url(url: Optional[str]) -> Optional[str]:
    """Sanitize URL by masking passwords and sensitive query parameter values."""
    if not url:
        return url
    try:
        parsed = urllib.parse.urlsplit(url)
        netloc = parsed.netloc
        if "@" in netloc:
            userinfo, host = netloc.split("@", 1)
            if ":" in userinfo:
                user, _ = userinfo.split(":", 1)
                netloc = f"{user}:***REDACTED***@{host}"
            else:
                netloc = f"***REDACTED***@{host}"

        query = parsed.query
        if query:
            parts = urllib.parse.parse_qsl(query, keep_blank_values=True)
            sensitive_keys = {
                "token", "key", "secret", "api_key", "apikey", "auth",
                "password", "signature", "access_token", "webhook", "pass"
            }
            sanitized_parts = []
            for k, v in parts:
                if any(s in k.lower() for s in sensitive_keys):
                    sanitized_parts.append((k, "***REDACTED***"))
                else:
                    sanitized_parts.append((k, v))
            query = urllib.parse.urlencode(sanitized_parts, safe="*")

        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    except Exception:
        return "***REDACTED_URL***"


def sanitize_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Return a copy of headers with sensitive authorization values masked."""
    if not headers:
        return {}
    sensitive_keys = {"authorization", "x-api-key", "token", "secret", "webhook-secret", "cookie"}
    sanitized = {}
    for k, v in headers.items():
        if any(s in k.lower() for s in sensitive_keys):
            sanitized[k] = "***REDACTED***"
        else:
            sanitized[k] = v
    return sanitized


def redact_sensitive_text(text: Optional[str]) -> Optional[str]:
    """Mask tokens, secrets, or keys inside error strings or log messages."""
    if not text:
        return text
    # Mask Bearer tokens
    text = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9_\-\.]+', r'\1***REDACTED***', text)
    # Mask key-value token patterns
    text = re.sub(r'(?i)(token|key|secret|password|api_key)=[^&\s]+', r'\1=***REDACTED***', text)
    return text


# -----------------------------------------------------------------------------
# Data Models and Enums
# -----------------------------------------------------------------------------

class AlertEventType(str, Enum):
    """Specific event triggering an alert."""

    NEW_QUALIFIED = "NEW_QUALIFIED"
    MATERIAL_CHANGE = "MATERIAL_CHANGE"
    DISQUALIFIED = "DISQUALIFIED"
    INVALIDATED = "INVALIDATED"
    DISAPPEARED = "DISAPPEARED"


class AlertDeliveryStatus(str, Enum):
    """Delivery lifecycle status for an alert."""

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class OpportunityAlert:
    """An auditable, immutable alert record generated for an execution-qualified candidate."""

    alert_id: str
    candidate_id: str
    event_type: AlertEventType
    timestamp: str  # ISO 8601 UTC
    strategy_type: str
    market_tickers: Tuple[str, ...]
    event_ticker: Optional[str]
    qualification_outcome: str
    gross_edge_cents: Optional[int]
    estimated_fees_cents: Optional[int]
    net_edge_cents: Optional[int]
    target_quantity: int
    executable_quantity: int
    total_cost_cents: Optional[int]
    guaranteed_payout_cents: Optional[int]
    return_on_capital_pct: Optional[str]
    data_freshness_seconds: Optional[float]
    message: str
    disclaimer: str = DEFAULT_ALERT_DISCLAIMER
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "candidate_id": self.candidate_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "strategy_type": self.strategy_type,
            "market_tickers": list(self.market_tickers),
            "event_ticker": self.event_ticker,
            "qualification_outcome": self.qualification_outcome,
            "gross_edge_cents": self.gross_edge_cents,
            "estimated_fees_cents": self.estimated_fees_cents,
            "net_edge_cents": self.net_edge_cents,
            "target_quantity": self.target_quantity,
            "executable_quantity": self.executable_quantity,
            "total_cost_cents": self.total_cost_cents,
            "guaranteed_payout_cents": self.guaranteed_payout_cents,
            "return_on_capital_pct": self.return_on_capital_pct,
            "data_freshness_seconds": self.data_freshness_seconds,
            "message": self.message,
            "disclaimer": self.disclaimer,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OpportunityAlert":
        return cls(
            alert_id=str(d["alert_id"]),
            candidate_id=str(d["candidate_id"]),
            event_type=AlertEventType(d["event_type"]),
            timestamp=str(d["timestamp"]),
            strategy_type=str(d["strategy_type"]),
            market_tickers=tuple(d.get("market_tickers", ())),
            event_ticker=d.get("event_ticker"),
            qualification_outcome=str(d.get("qualification_outcome", "UNKNOWN")),
            gross_edge_cents=d.get("gross_edge_cents"),
            estimated_fees_cents=d.get("estimated_fees_cents"),
            net_edge_cents=d.get("net_edge_cents"),
            target_quantity=int(d.get("target_quantity", 1)),
            executable_quantity=int(d.get("executable_quantity", 0)),
            total_cost_cents=d.get("total_cost_cents"),
            guaranteed_payout_cents=d.get("guaranteed_payout_cents"),
            return_on_capital_pct=d.get("return_on_capital_pct"),
            data_freshness_seconds=d.get("data_freshness_seconds"),
            message=str(d.get("message", "")),
            disclaimer=str(d.get("disclaimer", DEFAULT_ALERT_DISCLAIMER)),
            metadata=dict(d.get("metadata", {})),
        )


@dataclass(frozen=True)
class NotificationDeliveryRecord:
    """Record of an alert delivery attempt to a specific notification channel."""

    delivery_id: str
    alert_id: str
    channel: str
    status: AlertDeliveryStatus
    attempted_at: str
    completed_at: Optional[str]
    attempts: int
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "alert_id": self.alert_id,
            "channel": self.channel,
            "status": self.status.value,
            "attempted_at": self.attempted_at,
            "completed_at": self.completed_at,
            "attempts": self.attempts,
            "error_message": self.error_message,
        }


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class AlertConfig:
    """Configuration options for the alert and notification engine."""

    enabled: bool = True
    enabled_channels: Tuple[str, ...] = ("local_history",)
    webhook_url: Optional[str] = None
    webhook_timeout_seconds: float = 5.0
    webhook_max_retries: int = 3
    webhook_retry_backoff_seconds: float = 0.5
    desktop_notifications_enabled: bool = False
    profit_change_threshold_cents: int = 5
    quantity_change_threshold: int = 1
    cost_change_threshold_cents: int = 5
    re_alert_cooldown_seconds: float = 60.0
    state_file: Optional[Path] = None
    history_file: Optional[Path] = None
    max_queue_size: int = 1000
    max_history_records: int = 500

    def __post_init__(self) -> None:
        if self.webhook_timeout_seconds <= 0:
            raise ValueError("webhook_timeout_seconds must be positive")
        if self.webhook_max_retries < 0:
            raise ValueError("webhook_max_retries cannot be negative")
        if self.webhook_retry_backoff_seconds < 0:
            raise ValueError("webhook_retry_backoff_seconds cannot be negative")
        if self.profit_change_threshold_cents < 0:
            raise ValueError("profit_change_threshold_cents cannot be negative")
        if self.quantity_change_threshold < 0:
            raise ValueError("quantity_change_threshold cannot be negative")
        if self.re_alert_cooldown_seconds < 0:
            raise ValueError("re_alert_cooldown_seconds cannot be negative")
        if self.max_queue_size <= 0:
            raise ValueError("max_queue_size must be a positive integer")
        if self.max_history_records <= 0:
            raise ValueError("max_history_records must be a positive integer")


# -----------------------------------------------------------------------------
# Notification Channel Implementations
# -----------------------------------------------------------------------------

class BaseNotificationChannel(ABC):
    """Abstract base class for notification delivery channels."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Channel identifier."""

    @abstractmethod
    def send(self, alert: OpportunityAlert) -> bool:
        """Deliver the alert. Return True on success, False or raise on failure."""


class LocalHistoryChannel(BaseNotificationChannel):
    """Appends alerts to a persistent local JSONL history file."""

    def __init__(self, history_file_path: Optional[Path] = None) -> None:
        self.history_file_path = history_file_path or (
            Path("data") / "live_alert_history.jsonl"
        )
        self._lock = threading.Lock()
        self.history_file_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return "local_history"

    def send(self, alert: OpportunityAlert) -> bool:
        with self._lock:
            try:
                line = json.dumps(alert.to_dict()) + "\n"
                with open(self.history_file_path, "a", encoding="utf-8") as f:
                    f.write(line)
                return True
            except Exception as exc:
                logger.error("Failed writing alert to local history %s: %s", self.history_file_path, exc)
                raise


class DesktopNotificationChannel(BaseNotificationChannel):
    """Emits desktop notifications on macOS using osascript, with safe headless fallbacks."""

    def __init__(self) -> None:
        self.is_macos = sys.platform == "darwin"

    @property
    def name(self) -> str:
        return "desktop"

    def send(self, alert: OpportunityAlert) -> bool:
        if not self.is_macos:
            logger.debug("Desktop notifications not supported on non-macOS platform: %s", sys.platform)
            return True  # Gracefully no-op on non-macOS

        title = f"Kalshi Arbitrage: {alert.event_type.value}"
        subtitle = f"{alert.candidate_id} | Net: {alert.net_edge_cents or 0}¢ (Qty: {alert.executable_quantity})"
        body = alert.message.replace('"', '\\"').replace("'", "\\'")

        # macOS AppleScript notification
        script = f'display notification "{body}" with title "{title}" subtitle "{subtitle}" sound name "Glass"'
        cmd = ["osascript", "-e", script]

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0, check=False)
            if res.returncode != 0:
                logger.warning("osascript notification failed with code %d: %s", res.returncode, res.stderr.strip())
                return False
            return True
        except Exception as exc:
            logger.warning("Desktop notification dispatch error: %s", exc)
            return False


class WebhookNotificationChannel(BaseNotificationChannel):
    """Delivers alert payloads via HTTP POST webhook with retries, backoff, and secret masking."""

    def __init__(
        self,
        url: str,
        timeout_seconds: float = 5.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.5,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.url = url
        self.sanitized_url = sanitize_url(url)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.headers = headers or {}

    @property
    def name(self) -> str:
        return "webhook"

    def send(self, alert: OpportunityAlert) -> bool:
        payload = json.dumps(alert.to_dict()).encode("utf-8")
        req_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "KalshiArbitrageEngine/V24",
            **self.headers,
        }

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                req = urllib.request.Request(self.url, data=payload, headers=req_headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    if 200 <= resp.status < 300:
                        logger.info("Alert %s delivered to webhook %s (attempt %d)", alert.alert_id, self.sanitized_url, attempt)
                        return True
                    else:
                        last_error = f"HTTP {resp.status} response from webhook"
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}: {redact_sensitive_text(exc.reason)}"
            except urllib.error.URLError as exc:
                last_error = f"Network URLError: {redact_sensitive_text(str(exc.reason))}"
            except Exception as exc:
                last_error = f"Unexpected webhook error: {redact_sensitive_text(str(exc))}"

            if attempt < self.max_retries:
                sleep_dur = self.retry_backoff_seconds * (2 ** (attempt - 1))
                time.sleep(sleep_dur)

        logger.warning(
            "Webhook delivery failed for alert %s to %s after %d attempts: %s",
            alert.alert_id,
            self.sanitized_url,
            self.max_retries,
            last_error,
        )
        raise RuntimeError(f"Webhook delivery failed: {last_error}")


# -----------------------------------------------------------------------------
# Alert Engine
# -----------------------------------------------------------------------------

class AlertEngine:
    """Deterministic, asynchronous alert engine with deduplication and state persistence."""

    def __init__(
        self,
        config: Optional[AlertConfig] = None,
        time_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config or AlertConfig()
        self._time_provider = time_provider or (lambda: datetime.now(timezone.utc))

        # Channel registry
        self._channels: Dict[str, BaseNotificationChannel] = {}
        self._setup_channels()

        # Operational state
        self._is_running = False
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._delivery_queue: queue.Queue[Tuple[OpportunityAlert, str]] = queue.Queue(
            maxsize=self.config.max_queue_size
        )

        # Audit and telemetry
        self._lock = threading.Lock()
        self._alerts_created_count = 0
        self._alerts_delivered_count = 0
        self._alerts_failed_count = 0
        self._last_alert_time: Optional[str] = None
        self._last_delivery_time: Optional[str] = None
        self._channel_last_delivery_time: Dict[str, Optional[str]] = {
            c: None for c in self._channels.keys()
        }
        self._recent_delivery_errors: Deque[Dict[str, Any]] = deque(maxlen=20)
        self._alert_history: Deque[OpportunityAlert] = deque(maxlen=self.config.max_history_records)
        self._delivery_records: Deque[NotificationDeliveryRecord] = deque(maxlen=self.config.max_history_records)

        # Deduplication and candidate memory
        # Maps candidate_id -> tracking dict
        self._qualified_candidates_tracked: Dict[str, Dict[str, Any]] = {}

        # Load persisted state if available
        self._state_file_path = self.config.state_file or (Path("data") / "live_alert_state.json")
        self._load_state()

    def _now(self) -> datetime:
        return self._time_provider()

    def _setup_channels(self) -> None:
        """Initialize configured notification channels."""
        # 1. Local history channel
        if "local_history" in self.config.enabled_channels:
            hist_path = self.config.history_file or (Path("data") / "live_alert_history.jsonl")
            self._channels["local_history"] = LocalHistoryChannel(hist_path)

        # 2. Desktop notification channel
        if "desktop" in self.config.enabled_channels or self.config.desktop_notifications_enabled:
            self._channels["desktop"] = DesktopNotificationChannel()

        # 3. Webhook channel
        webhook_url = self.config.webhook_url or os.environ.get("KALSHI_WEBHOOK_URL")
        if ("webhook" in self.config.enabled_channels or webhook_url) and webhook_url:
            self._channels["webhook"] = WebhookNotificationChannel(
                url=webhook_url,
                timeout_seconds=self.config.webhook_timeout_seconds,
                max_retries=self.config.webhook_max_retries,
                retry_backoff_seconds=self.config.webhook_retry_backoff_seconds,
            )

    @property
    def is_running(self) -> bool:
        return self._is_running

    def start(self) -> None:
        """Start the background alert delivery worker thread."""
        with self._lock:
            if self._is_running:
                return
            self._is_running = True
            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._delivery_worker_loop,
                name="alert-delivery-worker",
                daemon=True,
            )
            self._worker_thread.start()
            logger.info("Alert engine background worker started with channels: %s", list(self._channels.keys()))

    def stop(self, timeout: float = 2.0) -> None:
        """Cleanly shutdown the alert worker thread and flush state."""
        with self._lock:
            if not self._is_running:
                return
            self._is_running = False
            self._stop_event.set()

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)

        # Save latest state on shutdown
        self._save_state()
        logger.info("Alert engine cleanly stopped. Undelivered in queue: %d", self._delivery_queue.qsize())

    def register_channel(self, channel: BaseNotificationChannel) -> None:
        """Register a custom notification delivery channel."""
        with self._lock:
            self._channels[channel.name] = channel
            self._channel_last_delivery_time[channel.name] = None

    # -------------------------------------------------------------------------
    # Core Deduplication & Candidate Evaluation Lifecycle
    # -------------------------------------------------------------------------

    def process_evaluator_cycle(
        self,
        active_candidates: Mapping[str, Any],
        transitions: Sequence[Any],
        now: Optional[datetime] = None,
    ) -> List[OpportunityAlert]:
        """Process candidate state from an evaluation cycle and emit deduplicated alerts.

        Rules:
        1. Only execution-qualified candidates (is_qualified=True) generate opportunity alerts.
        2. First-time qualification -> NEW_QUALIFIED alert.
        3. Continued qualification -> MATERIAL_CHANGE alert only if profit, quantity, or cost
           shifts beyond configured thresholds.
        4. Re-qualification after disqualification/disappearance -> NEW_QUALIFIED alert
           if re-alert cooldown has elapsed.
        5. Qualified candidate becomes unqualified/invalid -> DISQUALIFIED or INVALIDATED alert.
        6. Qualified candidate disappears -> DISAPPEARED alert (emitted exactly once).
        """
        if not self.config.enabled:
            return []

        curr_now = now or self._now()
        now_iso = curr_now.isoformat()
        emitted_alerts: List[OpportunityAlert] = []

        with self._lock:
            # 1. Process active candidates
            for cand_id, cand in active_candidates.items():
                is_qual = getattr(cand, "is_qualified", False)
                if not is_qual:
                    continue  # Unqualified candidates never trigger opportunity alerts!

                # Candidate is QUALIFIED
                prev_record = self._qualified_candidates_tracked.get(cand_id)

                net_edge = getattr(cand, "net_edge_cents", None)
                supported_qty = getattr(cand, "supported_quantity", 0)
                total_cost = getattr(cand, "total_cost_cents", None)

                if prev_record is None or not prev_record.get("is_currently_qualified", False):
                    # Newly qualified or re-qualified candidate
                    should_alert = True
                    if prev_record is not None:
                        # Check cooldown
                        last_alert_time_str = prev_record.get("last_alert_time")
                        if last_alert_time_str:
                            try:
                                last_alert_dt = datetime.fromisoformat(last_alert_time_str)
                                if (curr_now - last_alert_dt).total_seconds() < self.config.re_alert_cooldown_seconds:
                                    should_alert = False
                            except Exception:
                                pass

                    if should_alert:
                        alert = self._create_alert(
                            candidate=cand,
                            event_type=AlertEventType.NEW_QUALIFIED,
                            timestamp_iso=now_iso,
                            message=(
                                f"Newly qualified arbitrage opportunity detected on {cand_id}: "
                                f"Net Profit: {net_edge or 0}¢, Executable Qty: {supported_qty} contracts."
                            ),
                        )
                        emitted_alerts.append(alert)
                        self._dispatch_alert(alert)

                        # Update tracking record
                        self._qualified_candidates_tracked[cand_id] = {
                            "candidate_id": cand_id,
                            "strategy_type": getattr(cand, "strategy_type", "UNKNOWN"),
                            "is_currently_qualified": True,
                            "last_qualified_at": now_iso,
                            "last_alert_id": alert.alert_id,
                            "last_alert_type": AlertEventType.NEW_QUALIFIED.value,
                            "last_alert_time": now_iso,
                            "last_net_edge_cents": net_edge,
                            "last_executable_quantity": supported_qty,
                            "last_total_cost_cents": total_cost,
                            "disappeared_alert_emitted": False,
                        }
                    else:
                        # Cooldown active: alert suppressed. Do not mark is_currently_qualified=True
                        # so that once cooldown expires, the qualification alert will trigger.
                        if prev_record is not None:
                            prev_record["last_net_edge_cents"] = net_edge
                            prev_record["last_executable_quantity"] = supported_qty
                            prev_record["last_total_cost_cents"] = total_cost

                else:
                    # Candidate was already qualified in previous cycle. Check for material changes!
                    prev_net = prev_record.get("last_net_edge_cents")
                    prev_qty = prev_record.get("last_executable_quantity", 0)
                    prev_cost = prev_record.get("last_total_cost_cents")

                    delta_profit = abs((net_edge or 0) - (prev_net or 0)) if (net_edge is not None and prev_net is not None) else 0
                    delta_qty = abs(supported_qty - prev_qty)
                    delta_cost = abs((total_cost or 0) - (prev_cost or 0)) if (total_cost is not None and prev_cost is not None) else 0

                    is_material = (
                        delta_profit >= self.config.profit_change_threshold_cents
                        or delta_qty >= self.config.quantity_change_threshold
                        or delta_cost >= self.config.cost_change_threshold_cents
                    )

                    if is_material:
                        alert = self._create_alert(
                            candidate=cand,
                            event_type=AlertEventType.MATERIAL_CHANGE,
                            timestamp_iso=now_iso,
                            message=(
                                f"Material change on qualified candidate {cand_id}: "
                                f"Net Profit: {prev_net}¢ -> {net_edge}¢, Qty: {prev_qty} -> {supported_qty}."
                            ),
                            metadata={
                                "previous_net_edge_cents": prev_net,
                                "previous_executable_quantity": prev_qty,
                                "delta_profit_cents": delta_profit,
                                "delta_quantity": delta_qty,
                            },
                        )
                        emitted_alerts.append(alert)
                        self._dispatch_alert(alert)

                        # Update tracked snapshot
                        prev_record["last_alert_id"] = alert.alert_id
                        prev_record["last_alert_type"] = AlertEventType.MATERIAL_CHANGE.value
                        prev_record["last_alert_time"] = now_iso
                        prev_record["last_net_edge_cents"] = net_edge
                        prev_record["last_executable_quantity"] = supported_qty
                        prev_record["last_total_cost_cents"] = total_cost

            # 2. Process recorded transitions for candidates that were previously qualified
            for trans in transitions:
                cand_id = getattr(trans, "candidate_id", None)
                if not cand_id:
                    continue

                prev_record = self._qualified_candidates_tracked.get(cand_id)
                # Only alert on transitions if candidate was actually qualified!
                if prev_record is None or not prev_record.get("is_currently_qualified", False):
                    continue

                to_state = getattr(trans, "to_state", None)
                outcome = getattr(trans, "qualification_outcome", None)
                reason = getattr(trans, "reason", None)

                if to_state == "DISQUALIFIED":
                    alert = OpportunityAlert(
                        alert_id=f"alt-{uuid.uuid4().hex[:8]}",
                        candidate_id=cand_id,
                        event_type=AlertEventType.DISQUALIFIED,
                        timestamp=now_iso,
                        strategy_type=getattr(trans, "strategy_type", "UNKNOWN"),
                        market_tickers=tuple(getattr(trans, "market_tickers", ())),
                        event_ticker=None,
                        qualification_outcome=str(outcome or "DISQUALIFIED"),
                        gross_edge_cents=getattr(trans, "gross_edge_cents", None),
                        estimated_fees_cents=None,
                        net_edge_cents=getattr(trans, "net_edge_cents", None),
                        target_quantity=1,
                        executable_quantity=0,
                        total_cost_cents=None,
                        guaranteed_payout_cents=None,
                        return_on_capital_pct=None,
                        data_freshness_seconds=None,
                        message=f"Opportunity {cand_id} disqualified: {reason or outcome}",
                        disclaimer=DEFAULT_ALERT_DISCLAIMER,
                        metadata={"transition_id": getattr(trans, "transition_id", None), "reason": reason},
                    )
                    emitted_alerts.append(alert)
                    self._dispatch_alert(alert)
                    prev_record["is_currently_qualified"] = False
                    prev_record["last_disqualified_at"] = now_iso
                    prev_record["last_alert_id"] = alert.alert_id
                    prev_record["last_alert_type"] = AlertEventType.DISQUALIFIED.value
                    prev_record["last_alert_time"] = now_iso

                elif to_state == "INVALIDATED":
                    alert = OpportunityAlert(
                        alert_id=f"alt-{uuid.uuid4().hex[:8]}",
                        candidate_id=cand_id,
                        event_type=AlertEventType.INVALIDATED,
                        timestamp=now_iso,
                        strategy_type=getattr(trans, "strategy_type", "UNKNOWN"),
                        market_tickers=tuple(getattr(trans, "market_tickers", ())),
                        event_ticker=None,
                        qualification_outcome=str(outcome or "INVALIDATED"),
                        gross_edge_cents=getattr(trans, "gross_edge_cents", None),
                        estimated_fees_cents=None,
                        net_edge_cents=getattr(trans, "net_edge_cents", None),
                        target_quantity=1,
                        executable_quantity=0,
                        total_cost_cents=None,
                        guaranteed_payout_cents=None,
                        return_on_capital_pct=None,
                        data_freshness_seconds=None,
                        message=f"Opportunity {cand_id} invalidated: {reason or outcome}",
                        disclaimer=DEFAULT_ALERT_DISCLAIMER,
                        metadata={"transition_id": getattr(trans, "transition_id", None), "reason": reason},
                    )
                    emitted_alerts.append(alert)
                    self._dispatch_alert(alert)
                    prev_record["is_currently_qualified"] = False
                    prev_record["last_disqualified_at"] = now_iso
                    prev_record["last_alert_id"] = alert.alert_id
                    prev_record["last_alert_type"] = AlertEventType.INVALIDATED.value
                    prev_record["last_alert_time"] = now_iso

                elif to_state == "DISAPPEARED":
                    if not prev_record.get("disappeared_alert_emitted", False):
                        alert = OpportunityAlert(
                            alert_id=f"alt-{uuid.uuid4().hex[:8]}",
                            candidate_id=cand_id,
                            event_type=AlertEventType.DISAPPEARED,
                            timestamp=now_iso,
                            strategy_type=getattr(trans, "strategy_type", "UNKNOWN"),
                            market_tickers=tuple(getattr(trans, "market_tickers", ())),
                            event_ticker=None,
                            qualification_outcome="DISAPPEARED",
                            gross_edge_cents=getattr(trans, "gross_edge_cents", None),
                            estimated_fees_cents=None,
                            net_edge_cents=getattr(trans, "net_edge_cents", None),
                            target_quantity=1,
                            executable_quantity=0,
                            total_cost_cents=None,
                            guaranteed_payout_cents=None,
                            return_on_capital_pct=None,
                            data_freshness_seconds=None,
                            message=f"Qualified opportunity {cand_id} disappeared from active market observations.",
                            disclaimer=DEFAULT_ALERT_DISCLAIMER,
                            metadata={"reason": reason},
                        )
                        emitted_alerts.append(alert)
                        self._dispatch_alert(alert)
                        prev_record["is_currently_qualified"] = False
                        prev_record["disappeared_alert_emitted"] = True
                        prev_record["last_alert_id"] = alert.alert_id
                        prev_record["last_alert_type"] = AlertEventType.DISAPPEARED.value
                        prev_record["last_alert_time"] = now_iso

            # Periodically persist state
            if emitted_alerts:
                self._save_state()

        return emitted_alerts

    def _create_alert(
        self,
        candidate: Any,
        event_type: AlertEventType,
        timestamp_iso: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OpportunityAlert:
        """Construct an immutable OpportunityAlert from candidate attributes."""
        cand_id = getattr(candidate, "candidate_id", "UNKNOWN")
        strategy = getattr(candidate, "strategy_type", "UNKNOWN")
        tickers = tuple(getattr(candidate, "market_tickers", ()))
        event_ticker = getattr(candidate, "event_ticker", None)

        qual = getattr(candidate, "qualification", None)
        outcome = getattr(qual, "outcome", None) if qual else getattr(candidate, "status", "UNKNOWN")
        outcome_str = outcome.value if isinstance(outcome, Enum) else str(outcome)

        gross_edge = getattr(candidate, "gross_edge_cents", None)
        fees = getattr(candidate, "estimated_fees_cents", None)
        net_edge = getattr(candidate, "net_edge_cents", None)
        target_qty = getattr(candidate, "target_quantity", 1)
        exec_qty = getattr(candidate, "supported_quantity", 0)
        cost = getattr(candidate, "total_cost_cents", None)
        payout = getattr(candidate, "guaranteed_payout_cents", None)

        roc_pct = None
        freshness = None
        if qual:
            roc_pct = str(qual.return_on_capital_pct) if qual.return_on_capital_pct is not None else None
            freshness = qual.data_freshness_seconds

        alert_id = f"alt-{uuid.uuid4().hex[:8]}"

        return OpportunityAlert(
            alert_id=alert_id,
            candidate_id=cand_id,
            event_type=event_type,
            timestamp=timestamp_iso,
            strategy_type=strategy,
            market_tickers=tickers,
            event_ticker=event_ticker,
            qualification_outcome=outcome_str,
            gross_edge_cents=gross_edge,
            estimated_fees_cents=fees,
            net_edge_cents=net_edge,
            target_quantity=target_qty,
            executable_quantity=exec_qty,
            total_cost_cents=cost,
            guaranteed_payout_cents=payout,
            return_on_capital_pct=roc_pct,
            data_freshness_seconds=freshness,
            message=message,
            disclaimer=DEFAULT_ALERT_DISCLAIMER,
            metadata=dict(metadata or {}),
        )

    def _dispatch_alert(self, alert: OpportunityAlert) -> None:
        """Enqueue alert for delivery across all configured channels."""
        self._alerts_created_count += 1
        self._last_alert_time = alert.timestamp
        self._alert_history.append(alert)

        for channel_name in self._channels.keys():
            try:
                self._delivery_queue.put_nowait((alert, channel_name))
            except queue.Full:
                logger.warning("Alert delivery queue full! Dropping delivery of %s to %s", alert.alert_id, channel_name)
                self._alerts_failed_count += 1
                self._recent_delivery_errors.append({
                    "alert_id": alert.alert_id,
                    "channel": channel_name,
                    "error": "Delivery queue full (backpressure drop)",
                    "timestamp": alert.timestamp,
                })

    def _delivery_worker_loop(self) -> None:
        """Background thread worker pulling alerts from queue and dispatching to channels."""
        while not self._stop_event.is_set():
            try:
                item = self._delivery_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            alert, channel_name = item
            channel = self._channels.get(channel_name)
            if channel is None:
                self._delivery_queue.task_done()
                continue

            now_iso = self._now().isoformat()
            deliv_id = f"deliv-{uuid.uuid4().hex[:8]}"

            try:
                success = channel.send(alert)
                completed_iso = self._now().isoformat()

                if success:
                    with self._lock:
                        self._alerts_delivered_count += 1
                        self._last_delivery_time = completed_iso
                        self._channel_last_delivery_time[channel_name] = completed_iso
                        rec = NotificationDeliveryRecord(
                            delivery_id=deliv_id,
                            alert_id=alert.alert_id,
                            channel=channel_name,
                            status=AlertDeliveryStatus.DELIVERED,
                            attempted_at=now_iso,
                            completed_at=completed_iso,
                            attempts=1,
                            error_message=None,
                        )
                        self._delivery_records.append(rec)
                else:
                    with self._lock:
                        self._alerts_failed_count += 1
                        err_msg = "Channel returned delivery failure"
                        rec = NotificationDeliveryRecord(
                            delivery_id=deliv_id,
                            alert_id=alert.alert_id,
                            channel=channel_name,
                            status=AlertDeliveryStatus.FAILED,
                            attempted_at=now_iso,
                            completed_at=completed_iso,
                            attempts=1,
                            error_message=err_msg,
                        )
                        self._delivery_records.append(rec)
                        self._recent_delivery_errors.append({
                            "alert_id": alert.alert_id,
                            "channel": channel_name,
                            "error": err_msg,
                            "timestamp": completed_iso,
                        })

            except Exception as exc:
                completed_iso = self._now().isoformat()
                sanitized_err = redact_sensitive_text(str(exc))
                with self._lock:
                    self._alerts_failed_count += 1
                    rec = NotificationDeliveryRecord(
                        delivery_id=deliv_id,
                        alert_id=alert.alert_id,
                        channel=channel_name,
                        status=AlertDeliveryStatus.FAILED,
                        attempted_at=now_iso,
                        completed_at=completed_iso,
                        attempts=1,
                        error_message=sanitized_err,
                    )
                    self._delivery_records.append(rec)
                    self._recent_delivery_errors.append({
                        "alert_id": alert.alert_id,
                        "channel": channel_name,
                        "error": sanitized_err,
                        "timestamp": completed_iso,
                    })

            finally:
                self._delivery_queue.task_done()

        # Drain remaining items on shutdown before terminating
        while not self._delivery_queue.empty():
            try:
                item = self._delivery_queue.get_nowait()
            except queue.Empty:
                break
            alert, channel_name = item
            channel = self._channels.get(channel_name)
            if channel is not None:
                completed_iso = self._now().isoformat()
                try:
                    success = channel.send(alert)
                    with self._lock:
                        if success:
                            self._alerts_delivered_count += 1
                            self._last_delivery_time = completed_iso
                            self._channel_last_delivery_time[channel_name] = completed_iso
                        else:
                            self._alerts_failed_count += 1
                except Exception:
                    with self._lock:
                        self._alerts_failed_count += 1
            self._delivery_queue.task_done()

    # -------------------------------------------------------------------------
    # Telemetry and State Persistence
    # -------------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Expose operational telemetry for the alert engine."""
        with self._lock:
            status = "RUNNING" if self._is_running else ("DISABLED" if not self.config.enabled else "STOPPED")
            return {
                "status": status,
                "is_running": self._is_running,
                "enabled": self.config.enabled,
                "enabled_channels": list(self._channels.keys()),
                "alerts_created_count": self._alerts_created_count,
                "alerts_delivered_count": self._alerts_delivered_count,
                "alerts_failed_count": self._alerts_failed_count,
                "pending_queue_depth": self._delivery_queue.qsize(),
                "last_alert_time": self._last_alert_time,
                "last_delivery_time": self._last_delivery_time,
                "channel_last_delivery_time": dict(self._channel_last_delivery_time),
                "tracked_candidates_count": len(self._qualified_candidates_tracked),
                "currently_qualified_count": sum(
                    1 for c in self._qualified_candidates_tracked.values() if c.get("is_currently_qualified")
                ),
                "recent_delivery_errors": list(self._recent_delivery_errors)[-10:],
                "config": {
                    "profit_change_threshold_cents": self.config.profit_change_threshold_cents,
                    "quantity_change_threshold": self.config.quantity_change_threshold,
                    "re_alert_cooldown_seconds": self.config.re_alert_cooldown_seconds,
                    "webhook_configured": bool(self.config.webhook_url or os.environ.get("KALSHI_WEBHOOK_URL")),
                    "desktop_enabled": "desktop" in self._channels,
                },
            }

    def get_recent_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent alerts in reverse chronological order."""
        with self._lock:
            alerts = [a.to_dict() for a in self._alert_history]
            alerts.reverse()
            return alerts[:limit]

    def _save_state(self) -> None:
        """Persist alert engine state and deduplication tracking atomically."""
        if not self._state_file_path:
            return

        tmp_path = self._state_file_path.with_suffix(".tmp")
        try:
            self._state_file_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "status": "RUNNING" if self._is_running else "STOPPED",
                "saved_at": self._now().isoformat(),
                "alerts_created_count": self._alerts_created_count,
                "alerts_delivered_count": self._alerts_delivered_count,
                "alerts_failed_count": self._alerts_failed_count,
                "last_alert_time": self._last_alert_time,
                "last_delivery_time": self._last_delivery_time,
                "channel_last_delivery_time": dict(self._channel_last_delivery_time),
                "qualified_candidates_tracked": self._qualified_candidates_tracked,
                "recent_delivery_errors": list(self._recent_delivery_errors)[-20:],
                "config": {
                    "enabled_channels": list(self._channels.keys()),
                    "profit_change_threshold_cents": self.config.profit_change_threshold_cents,
                    "quantity_change_threshold": self.config.quantity_change_threshold,
                    "re_alert_cooldown_seconds": self.config.re_alert_cooldown_seconds,
                },
            }
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            tmp_path.replace(self._state_file_path)
        except Exception as exc:
            logger.warning("Failed to persist alert state to %s: %s", self._state_file_path, exc)

    def _load_state(self) -> None:
        """Load persisted state to avoid duplicate notifications on restart."""
        if not self._state_file_path or not self._state_file_path.exists():
            return

        try:
            with open(self._state_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._alerts_created_count = int(data.get("alerts_created_count", 0))
            self._alerts_delivered_count = int(data.get("alerts_delivered_count", 0))
            self._alerts_failed_count = int(data.get("alerts_failed_count", 0))
            self._last_alert_time = data.get("last_alert_time")
            self._last_delivery_time = data.get("last_delivery_time")
            for ch, ts in data.get("channel_last_delivery_time", {}).items():
                self._channel_last_delivery_time[ch] = ts

            self._qualified_candidates_tracked = dict(data.get("qualified_candidates_tracked", {}))
            for err in data.get("recent_delivery_errors", []):
                self._recent_delivery_errors.append(err)

            logger.info(
                "Restored alert engine state from %s (tracked candidates: %d, delivered: %d)",
                self._state_file_path,
                len(self._qualified_candidates_tracked),
                self._alerts_delivered_count,
            )
        except Exception as exc:
            logger.warning("Could not load persisted alert state from %s: %s", self._state_file_path, exc)
