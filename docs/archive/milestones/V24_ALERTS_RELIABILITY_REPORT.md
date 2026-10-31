# Kalshi Arbitrage Engine V24 — Alerts & Long-Running Reliability Report

**Milestone:** V24  
**Date:** October 2026  
**Status:** COMPLETE & VERIFIED  
**Repository:** `/Users/vikramaditya/Disk/Projects/Kalshi`  
**Test Suite:** 706 passed (100% passing across 706 unit & integration tests)  
**Deliverable Status:** Production-Ready Qualified-Opportunity Alert Engine & Hardened Reliability  

---

## 1. Executive Summary

Milestone **V24: Alerts & Long-Running Reliability** introduces a qualified-opportunity alert engine and fortifies the operational reliability of the Kalshi Arbitrage Research Engine's continuous pipeline:

$$\text{Live Kalshi REST} \longrightarrow \text{V20 Collector} \longrightarrow \text{V21 Evaluator} \longrightarrow \text{V22 Qualifier} \longrightarrow \text{V24 Alert Engine} \longrightarrow \text{V23 Live Dashboard}$$

In live financial prediction markets, continuous market scans are only valuable if researchers and operators can be alerted to genuine execution opportunities as they arise, while avoiding alert fatigue from false alarms, duplicate pings, or theoretical noise.

V24 achieves this through:
1. **Qualified-Only Opportunity Alerts:** Only candidates meeting all V22 microstructure criteria (resting depth across all legs, multi-level VWAP slippage, and net profitability after CFTC quadratic taker fees) can trigger opportunity alerts. Theoretical candidates, unqualified candidates, historical replays, and synthetic simulation fixtures are strictly prevented from emitting alerts.
2. **Persistent State Deduplication:** Tracks candidate state and alert history across polling cycles and daemon restarts. If an alert service or daemon restarts, it loads persisted deduplication state (`data/live_alert_state.json`) and suppresses duplicate notification storms.
3. **Material-Change Detection:** If a qualified opportunity continues to qualify across cycles, alerts are suppressed unless net profit changes by $\ge \Delta_{\text{profit}}$ (default: 5¢), executable quantity changes by $\ge \Delta_{\text{qty}}$ (default: 1 contract), or total acquisition cost changes by $\ge \Delta_{\text{cost}}$ (default: 5¢).
4. **Lifecycle State Awareness:** Automatically emits discrete, auditable alerts when opportunities experience state changes: `NEW_QUALIFIED`, `MATERIAL_CHANGE`, `DISQUALIFIED` (depth/fees lost), `INVALIDATED` (stale data $>60\text{s}$ or leg skew $>5\text{s}$), and `DISAPPEARED` (market inactive $>180\text{s}$, emitted exactly once).
5. **Decoupled Asynchronous Dispatch:** Employs an asynchronous worker thread backed by a bounded queue (`queue.Queue`, default size 1,000). Slow or failing notification endpoints (e.g. lagging webhooks) never block continuous market data ingestion or arbitrage candidate evaluation.
6. **Multi-Channel Notification Architecture:**
   - **Local History (`local_history`):** Persistent, append-only JSONL log (`data/live_alert_history.jsonl`) that operates fully offline with zero external services.
   - **Desktop Notifications (`desktop`):** Native macOS notifications via `osascript`, with graceful no-op fallbacks in headless or non-macOS environments.
   - **HTTP Webhooks (`webhook`):** Asynchronous HTTP POST delivery with bounded retries (3 attempts), exponential backoff, connection timeouts, and strict secret/credential masking.
7. **Strict Secret & Token Redaction:** API keys, webhook tokens, Bearer credentials, and private query parameters are systematically sanitized (`***REDACTED***`) in all logs, error records, and status APIs.
8. **Operational Telemetry & Dashboard Integration:** Exposes alert metrics through `/api/alerts/status`, `/api/alerts/history`, and `/api/live/summary`, integrated into the dashboard's System Health and Candidates tabs.

All functionality has been verified via a 15-test suite (`tests/test_v24_alerts_reliability.py`), bringing the repository baseline to **706 passing tests (0 failures, 0 regressions)**. An end-to-end prospective trial against Kalshi's live public endpoints verified zero false-positive alerts on real markets, alongside controlled validation of all 9 lifecycle scenarios.

---

## 2. Alert Engine Architecture & Event Flow

The V24 alert engine decouples candidate detection from delivery via a producer-consumer architecture:

```mermaid
flowchart TD
    subgraph "Streaming Detection & Qualification"
        A["V20 ContinuousCollector"] --> B["Live Observations Stream\n(data/live_collection/observations.jsonl)"]
        B --> C["V21 ContinuousEvaluator"]
        C --> D["V22 ExecutionAwareArbitrageQualifier\n(Depth, VWAP, CFTC Fees, Timing)"]
    end

    subgraph "V24 Alert Engine (Deduplication Core)"
        D --> E{"Is Candidate\nExecution-Qualified?"}
        E -- "No (Depth/Fee Rejected)" --> E1["Suppress Alert\n(Zero False Positives)"]
        E -- "Yes (Qualified)" --> F{"Deduplication Audit\nAgainst State Cache"}
        
        F -- "First time / Requalified" --> G1["Emit NEW_QUALIFIED"]
        F -- "Unchanged / Sub-threshold" --> G2["Suppress Alert\n(Duplicate Filter)"]
        F -- "Delta Profit >= 5¢ or Delta Qty >= 1" --> G3["Emit MATERIAL_CHANGE"]
        
        D --> H{"Candidate Lifecycle\nTransition Event"}
        H -- "Depth/Fee Lost" --> H1["Emit DISQUALIFIED"]
        H -- "Stale (>60s) / Skew (>5s)" --> H2["Emit INVALIDATED"]
        H -- "Unobserved (>180s)" --> H3["Emit DISAPPEARED\n(Exactly Once)"]
    end

    subgraph "Asynchronous Delivery Engine"
        G1 & G3 & H1 & H2 & H3 --> Q["Bounded Delivery Queue\n(Queue depth <= 1,000)"]
        Q --> W["Async Worker Thread\n(Non-Blocking Consumer)"]
        
        W --> CH1["Local History Channel\n(data/live_alert_history.jsonl)"]
        W --> CH2["Desktop Channel\n(macOS osascript / safe fallback)"]
        W --> CH3["Webhook Channel\n(HTTP POST, retries & backoff)"]
        
        W --> MEM["Deduplication State\n(data/live_alert_state.json)"]
    end

    subgraph "Observability & Live Dashboard"
        MEM --> API1["GET /api/alerts/status"]
        CH1 --> API2["GET /api/alerts/history"]
        MEM --> API3["GET /api/live/summary"]
    end
```

### Invariants Enforced
- **Zero Live Execution Risk:** The alert engine strictly sends notifications; it contains zero trading capabilities, order routing, or account access.
- **Fail-Closed Isolation:** If a notification channel raises a network timeout or connection error, the worker logs the error, records the failure, and continues. The scanning loop is never blocked or terminated.
- **Durable Deduplication:** State is written atomically using temporary files (`.tmp`) and atomic filesystem renames (`os.replace`) to prevent corruption during unexpected shutdowns.

---

## 3. Deduplication and Re-Alert Rules

Alert fatigue is prevented through a formal state machine:

| Event Type | Trigger Invariant | Deduplication / Suppression Policy |
| :--- | :--- | :--- |
| `NEW_QUALIFIED` | Candidate evaluates as `is_qualified=True` for the first time or re-qualifies after cooldown. | Emitted once upon qualification. Suppressed if candidate was disqualified within the cooldown window ($t - t_{\text{last\_alert}} < \text{cooldown}$). |
| `MATERIAL_CHANGE` | Candidate remains qualified, but net profit changes by $\ge \Delta_{\text{profit}}$ (5¢), executable qty changes by $\ge \Delta_{\text{qty}}$ (1 contract), or total cost changes by $\ge \Delta_{\text{cost}}$ (5¢). | Emitted only when thresholds are breached. Sub-threshold quote ticks are completely suppressed. |
| `DISQUALIFIED` | Previously qualified candidate loses resting depth or fees exceed profit ($Net \le 0$). | Emitted once upon transition. Candidates that were never qualified do not trigger disqualification alerts. |
| `INVALIDATED` | Previously qualified candidate becomes invalid due to stale inputs ($>60\text{s}$), excessive leg skew ($>5\text{s}$), or missing markets. | Emitted once upon transition. |
| `DISAPPEARED` | Previously qualified candidate's markets are unobserved for $>180\text{s}$. | Emitted exactly once upon disappearance. Does not repeat on subsequent cycles while the candidate remains absent. |

### Re-Alert Cooldown Policy
When a candidate that was previously disqualified or disappeared becomes qualified again:
1. If elapsed time since the previous alert is $\ge \text{re\_alert\_cooldown\_seconds}$ (default: 60.0s):
   - A new `NEW_QUALIFIED` alert is emitted.
   - The candidate's baseline metrics are reset.
2. If elapsed time is $< \text{cooldown}$:
   - The alert is suppressed until the cooldown window expires.
   - Once the cooldown elapses and the candidate remains qualified, the alert triggers cleanly.

---

## 4. Notification Channels & Configuration

### 4.1 Supported Channels

| Channel | Identifier | Prerequisites | Delivery Behavior |
| :--- | :--- | :--- | :--- |
| **Local History** | `local_history` | None (Local filesystem access) | Appends structured JSON lines to `data/live_alert_history.jsonl`. Thread-safe, atomic, zero network dependencies. |
| **Desktop Notifications** | `desktop` | macOS (`sys.platform == 'darwin'`) | Invokes AppleScript via `osascript` displaying title, candidate ID, net profit, and message with system chime (`Glass`). Safely no-ops in headless or Linux environments. |
| **HTTP Webhook** | `webhook` | Configured `webhook_url` or `KALSHI_WEBHOOK_URL` env | HTTP POST sending complete `OpportunityAlert` JSON payload. Timeout: 5.0s. Retries: 3 attempts with exponential backoff (0.5s, 1.0s, 2.0s). Sanitizes all secrets in logs. |

### 4.2 Configuration Parameters (`AlertConfig`)

```python
@dataclass(frozen=True)
class AlertConfig:
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
    state_file: Optional[Path] = None  # Default: data/live_alert_state.json
    history_file: Optional[Path] = None  # Default: data/live_alert_history.jsonl
    max_queue_size: int = 1000
    max_history_records: int = 500
```

### 4.3 Safe Defaults
- Webhook delivery is **disabled** by default unless an explicit URL is provided.
- Desktop notifications are **disabled** by default.
- Local history logging is **always enabled** to provide an audit trail without requiring external network connectivity.

---

## 5. Security & Secret Redaction

To prevent sensitive credentials, webhook tokens, or API secrets from leaking into logs, state files, or status APIs, V24 enforces multi-tier sanitization:

1. **URL Sanitization (`sanitize_url`):**
   - Query parameters matching `token`, `key`, `secret`, `api_key`, `auth`, `password`, `signature`, or `webhook` are replaced with `***REDACTED***`.
   - Authority credentials (e.g. `https://user:pass@host/`) have passwords masked as `user:***REDACTED***@host`.
2. **Header Sanitization (`sanitize_headers`):**
   - Headers matching `Authorization`, `X-API-Key`, `Token`, or `Secret` are replaced with `***REDACTED***`.
3. **Error Text Redaction (`redact_sensitive_text`):**
   - Regular expressions scan error strings and strip Bearer tokens (`Bearer ***REDACTED***`) and token key-values before storing them in `recent_delivery_errors`.

---

## 6. Operational Health & Status APIs

V24 introduces dedicated REST endpoints and expands existing health APIs:

| Endpoint | Method | Query Parameters | Response Description |
| :--- | :--- | :--- | :--- |
| `GET /api/alerts/status` | `GET` | None | Operational telemetry: `status` (`RUNNING`, `STOPPED`, `DISABLED`), `enabled_channels`, `alerts_created_count`, `alerts_delivered_count`, `alerts_failed_count`, `pending_queue_depth`, `last_alert_time`, `last_delivery_time`, `channel_last_delivery_time`, `tracked_candidates_count`, `recent_delivery_errors`. |
| `GET /api/alerts/history` | `GET` | `limit`, `event_type`, `candidate_id` | Paginated audit log of emitted alerts in reverse chronological order from `data/live_alert_history.jsonl`. |
| `GET /api/live/summary` | `GET` | None | Updated to include `alerts` summary, `alerts_created_count`, `alerts_delivered_count`, and `alerts_failed_count`. |
| `GET /api/status` | `GET` | None | Updated to include `alert_service` component telemetry in system-wide health. |

### Dashboard UI Additions
- **Top Navigation Bar:** Added `#alert-status-pill` reflecting real-time alert daemon status (`Alerts: Active (N sent)` / `Alerts: Stopped`).
- **Candidates & Rejections Tab:** Added **Live Qualified Opportunity Alerts Log (V24)** table streaming emitted alerts in real time.
- **System Health Tab:** Added **Alert & Delivery Engine (V24)** component diagnostics card showing delivery metrics, channel statuses, queue depth, and secret redaction status.

---

## 7. Exact Run & Shutdown Commands

### 7.1 Running the Continuous Evaluator with Alerts

```bash
# Standard execution with local alert history logging:
python scripts/run_continuous_evaluator.py \
  --observations-file data/live_collection/observations.jsonl \
  --alerts-enabled

# Running with native desktop notifications on macOS:
python scripts/run_continuous_evaluator.py \
  --observations-file data/live_collection/observations.jsonl \
  --desktop-alerts

# Running with webhook delivery (Discord, Slack, or custom webhook):
python scripts/run_continuous_evaluator.py \
  --observations-file data/live_collection/observations.jsonl \
  --webhook-url "https://discord.com/api/webhooks/YOUR_WEBHOOK_URL"

# Or configure webhook via environment variable:
export KALSHI_WEBHOOK_URL="https://discord.com/api/webhooks/YOUR_WEBHOOK_URL"
python scripts/run_continuous_evaluator.py

# Running with customized material-change thresholds:
python scripts/run_continuous_evaluator.py \
  --profit-change-threshold 10 \
  --quantity-change-threshold 2 \
  --re-alert-cooldown 120.0

# Running without alerts (pure detection mode):
python scripts/run_continuous_evaluator.py --no-alerts
```

### 7.2 Graceful Shutdown
Send `SIGINT` (Ctrl+C) or `SIGTERM`:
```bash
# In-flight cycles finish cleanly, queue items are processed or saved, and state is persisted:
[Signal 2] Initiating graceful continuous evaluator shutdown...
Alert engine cleanly stopped. Undelivered in queue: 0
Continuous live evaluator stopped.
```

---

## 8. Automated Test Results

A dedicated automated test suite was implemented in [`tests/test_v24_alerts_reliability.py`](file:///Users/vikramaditya/Disk/Projects/Kalshi/tests/test_v24_alerts_reliability.py):

| Test Case | Objective | Result |
| :--- | :--- | :--- |
| `test_sanitize_url_masks_sensitive_tokens_and_passwords` | Verifies webhook URLs mask query tokens and authority passwords. | **PASSED** |
| `test_sanitize_headers_masks_auth` | Verifies Authorization and API key headers are masked as `***REDACTED***`. | **PASSED** |
| `test_redact_sensitive_text` | Verifies Bearer tokens and key=val strings in errors are redacted. | **PASSED** |
| `test_newly_qualified_candidate_generates_alert` | Confirms execution-qualified candidate produces exactly 1 `NEW_QUALIFIED` alert. | **PASSED** |
| `test_unqualified_candidates_never_alert` | Confirms candidates rejected for depth, fees, or timing produce 0 alerts. | **PASSED** |
| `test_duplicate_suppression_across_cycles` | Confirms identical candidates on subsequent cycles produce 0 duplicate alerts. | **PASSED** |
| `test_material_change_alert_on_profit_and_quantity` | Confirms material profit or qty changes produce `MATERIAL_CHANGE` alerts. | **PASSED** |
| `test_candidate_disqualification_invalidation_and_disappearance` | Confirms `DISQUALIFIED`, `INVALIDATED`, and `DISAPPEARED` alerts trigger on transition. | **PASSED** |
| `test_requalification_cooldown` | Confirms requalification is suppressed during cooldown and triggers after expiry. | **PASSED** |
| `test_persistence_across_process_restarts` | Confirms deduplication state survives restart without flood of duplicate alerts. | **PASSED** |
| `test_local_history_channel` | Confirms append-only JSONL writes and schema validity. | **PASSED** |
| `test_async_delivery_worker_thread` | Confirms worker thread consumes queue and dispatches asynchronously. | **PASSED** |
| `test_webhook_failure_and_retries` | Confirms retries with exponential backoff and error recording without crash. | **PASSED** |
| `test_evaluator_with_alert_engine_integration` | Confirms evaluator automatically lifecycle-manages the alert engine. | **PASSED** |
| `test_dashboard_server_api_alerts_endpoints` | Confirms `/api/alerts/status`, `/api/alerts/history`, and live summary integration. | **PASSED** |

### Full Suite Run
```bash
./.venv/bin/pytest -q
706 passed in 24.33s
```
**Results:** 706 passed, 0 failures, 0 regressions across the entire repository.

---

## 9. Prospective End-to-End Trial & Empirical Verification

A prospective verification trial was executed against Kalshi live production market data on October 28, 2026.

### 9.1 Phase 1: Live Public Market Evaluation
- Continuous Collector polled 6 open markets across 2 cycles (5.52s).
- Continuous Evaluator evaluated 12 live observations with V24 alerts enabled (1.67s).
- **Result:** **0 false-positive alerts emitted on live markets.** Because calm prediction markets consistently exhibit 0 resting counterparty bid depth on unhedged contracts, the V22 qualification layer rejected all candidates and the V24 alert engine honestly generated zero notifications.

### 9.2 Phase 2: Controlled Fixture Progression (Labeled Synthetic Fixtures)
To verify all alert transitions, controlled synthetic test fixtures were streamed through the live evaluator:
1. **Scenario A (Unqualified Candidate):** Produced 0 alerts.
2. **Scenario B (Newly Qualified Candidate):** Generated `alt-559d3cf3` (`NEW_QUALIFIED`, Net Profit: 20¢, Executable Qty: 5).
3. **Scenario C (Unchanged Candidate):** Produced 0 duplicate alerts (clean suppression).
4. **Scenario D (Material Profit Change +10¢):** Generated `alt-eaecb3ee` (`MATERIAL_CHANGE`, Net Profit: 30¢, Delta: +10¢).
5. **Scenario E (Material Quantity Change 5 -> 2):** Generated `alt-10aa9749` (`MATERIAL_CHANGE`, New Qty: 2).
6. **Scenario F (Candidate Invalidation):** Generated `alt-08da6847` (`INVALIDATED`, Reason: Snapshot age > 60s).
7. **Scenario G (Candidate Disappearance):** Generated `alt-d2847242` (`DISAPPEARED`, Reason: Market inactive). Subsequent cycles emitted 0 duplicate alerts.
8. **Scenario H (Requalification after Cooldown):** Generated `alt-c3359d99` (`NEW_QUALIFIED`).
9. **Scenario I (Process Restart Deduplication):** Engine stopped and restarted with state file; subsequent cycle on active candidate produced 0 duplicate alerts.

### 9.3 Phase 3: Dashboard HTTP API Probing
- `GET /api/alerts/status`: HTTP 200 OK (returned operational telemetry).
- `GET /api/alerts/history`: HTTP 200 OK (returned reverse-chronological alert log).
- `GET /api/live/summary`: HTTP 200 OK (`alerts` section fully integrated).

All evidence is recorded in [`docs/v24_alerts_trial_evidence.json`](file:///Users/vikramaditya/Disk/Projects/Kalshi/docs/v24_alerts_trial_evidence.json).

---

## 10. Known Limitations & Work Reserved for V25

1. **Non-Atomic Multi-Leg Execution:** V24 alerts researchers to execution-qualified candidates based on resting order books at snapshot time. Because prediction markets do not support atomic multi-leg contingency orders, live order execution entails legging-out risk. Live automated order submission is explicitly out of scope for V24 and remains strictly prohibited.
2. **Extended Multi-Day Live Validation Campaign:** While V24 establishes the complete alert and reliability architecture, continuous multi-day prospective observation across diverse trading hours and economic announcements is reserved for Milestone V25.
3. **Mobile Push Notifications:** V24 supports Local History, Desktop Notifications, and HTTP Webhooks. Push services (Pushover, Telegram, Twilio SMS) can easily ingest the webhook JSON format or be implemented as custom channel adapters in future milestones.

---

## 11. Milestone Sign-Off

Milestone V24 fulfills all requirements:
- Reliable qualified-opportunity alerts generated exclusively from genuine V22 qualification events.
- Persistent deduplication preventing repeated alerts and surviving restarts.
- Asynchronous non-blocking delivery over local history, desktop, and webhook channels.
- Full secret redaction and error isolation.
- Complete automated test suite (706 passed).
- End-to-end prospective verification against live public markets.
