# Stage 5 — Execution Architecture & Risk Controls Specification

**Date:** October 30, 2026  
**System:** Kalshi Breadth-First Arbitrage Engine  
**Repository:** `/Users/vikramaditya/Disk/Projects/Kalshi`  
**Git Branch:** `fix/v19-clean-checkout-test`  
**Author:** Antigravity Autonomous Audit Agent  
**Operational Mode:** STRICTLY READ-ONLY (Design & Simulation Only — Zero Live Order Capabilities)

---

## 1. Executive Summary

This document presents the technical design and formal risk management architecture for potential future execution of multi-leg arbitrage on the Kalshi exchange.

Prediction market arbitrage involves unique microstructure risks: Kalshi does **not** support native multi-leg atomic spread or basket orders. Every leg must be submitted, tracked, and filled as an independent order. If market liquidity shifts or prices move while an order is in-flight, an unhedged directional position ("legging risk") is created.

This specification details:
1. **The Asynchronous Multi-Leg Execution Architecture.**
2. **Deterministic Pre-Trade Risk Controls & Kill Switches.**
3. **Leg Sequencing & Emergency Unwind Policies.**
4. **Architectural Isolation:** Strict physical and software separation preserving the read-only safety boundary of the scanner.

---

## 2. Asynchronous Multi-Leg Execution Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Read-Only Discovery Engine                          │
│                                                                             │
│  Stage A: Discovery ──> Stage B: Screening ──> Stage C: Targeted Qualifier  │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ Qualified Candidate Event
                                       ▼ (JSON Message via Internal IPC/Bus)
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Execution Orchestration Engine (Isolated)                │
│                                                                             │
│  ┌─────────────────────────┐          ┌──────────────────────────────────┐  │
│  │   Pre-Trade Risk Gate   │──Pass───>│     Multi-Leg Order Router       │  │
│  │ (Capital, Position, ROC)│          │  (Sequencer: Illiquid-Leg First) │  │
│  └───────────┬─────────────┘          └────────────────┬─────────────────┘  │
│              │ Reject                                  │ Orders             │
│              ▼                                         ▼                    │
│      Audit Rejection Log                      Kalshi REST / WS FIX          │
│                                                        │ Execution Stream   │
│                                                        ▼                    │
│                                       ┌──────────────────────────────────┐  │
│                                       │   Asynchronous Lifecycle Manager │  │
│                                       │ (PLANNED -> EXECUTING -> FILLED) │  │
│                                       └────────────────┬─────────────────┘  │
│                                                        │ Incomplete Fill    │
│                                                        ▼                    │
│                                       ┌──────────────────────────────────┐  │
│                                       │     Unwind & Scratch Engine      │  │
│                                       │ (EMERGENCY_DUMP / AGGRESSIVE_X)  │  │
│                                       └──────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 State Machine Lifecycle
As implemented in `kalshi_arbitrage/async_execution.py`, a multi-leg trade progresses through explicit, deterministic states:
1. **`PLANNED`:** Candidate passed Stage C qualification and pre-trade risk checks.
2. **`EXECUTING`:** Orders actively dispatched across constituent legs.
3. **`PARTIALLY_FILLED`:** One or more legs executed, but full target quantity not reached across all legs.
4. **`FILLED`:** 100% of target contracts filled across all legs. Guaranteed arbitrage locked.
5. **`LEGGED_OUT`:** An in-flight leg timed out, rejected, or ran out of depth, creating unhedged residual exposure.
6. **`UNWINDING`:** Automated recovery policy executing to eliminate directional exposure.
7. **`CLOSED`:** Trade resolved, either through complete settlement or post-unwind scratching.
8. **`FAILED`:** Pre-trade validation failure or irrecoverable API transport error.

### 2.2 Leg Sequencing Policies
Because orders execute sequentially over network hops:
- **`ILLIQUID_FIRST` (Recommended):** Submit the order on the thinnest, highest-slippage leg first. If that order fails or is only partially filled, abort immediately before committing capital to liquid legs.
- **`HIGH_PRICE_FIRST`:** Order the highest capital commitment leg first.
- **`SIMULTANEOUS`:** Dispatch all legs concurrently using an asynchronous worker pool, requiring strict sub-50ms round-trip times.

---

## 3. Pre-Trade Risk Management & Limits

The pre-trade risk engine (`kalshi_arbitrage/risk.py`) enforces strict boundaries before an order can enter the execution pipeline:

| Control Parameter | Boundary Limit | Purpose |
| :--- | :---: | :--- |
| **Max Capital per Trade** | $\$100.00$ ($10,000$ cents) | Caps loss exposure on any individual basket. |
| **Max Aggregate Exposure** | $\$1,000.00$ ($100,000$ cents) | Caps total open capital committed across all active positions. |
| **Max Position per Contract** | $50$ contracts | Prevents concentration risk in illiquid outcomes. |
| **Minimum Net Profit** | $\ge \$0.05$ per contract | Ensures edge survives slippage and taker fees. |
| **Maximum Leg Count** | $\le 8$ legs | Bounds structural complexity and execution failure surface. |
| **Maximum Permissible Skew** | $\le 2.0$ seconds | Rejects stale cross-leg quote sets. |
| **Daily Loss Limit Kill Switch** | $\$50.00$ cumulative loss | Automated hard stop halting all execution for 24 hours. |

---

## 4. Execution Risks & Mitigation Protocols

### 4.1 Legging Risk & Unwind Policies
If Leg 1 fills 10 contracts but Leg 2 order is rejected due to book depletion:
- **`EMERGENCY_DUMP` Policy:** Immediately submit a market sell order on Leg 1 to liquidate the open position at the prevailing bid, accepting a small scratch loss to eliminate overnight directional risk.
- **`AGGRESSIVE_CROSS` Policy:** If depth exists at the next ask level and total basket cost still yields $\text{Net Profit} > 0$, cross the spread on Leg 2 to complete the hedge.

### 4.2 Idempotency & Order Duplicate Protection
- Every multi-leg trade generates a unique deterministic `client_order_id` incorporating timestamp, session ID, and candidate stable key.
- Kalshi REST API `POST /portfolio/orders` requires client order deduplication to prevent double fills during network retries.

---

## 5. Safety & Architectural Separation

To protect production safety:
1. **Scanner Boundary:** `scripts/run_breadth_scanner.py` and `kalshi_arbitrage/breadth_scanner.py` operate exclusively with public read-only endpoints. The scanner contains zero order routing code and zero authentication keys.
2. **Credential Isolation:** API trading keys (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY`) must never be loaded into scanner processes or persisted in scanner configuration files.
3. **Independent Runtime:** Any future execution harness must run as a distinct process communicating with the scanner via read-only event logs or read-only IPC queues.
