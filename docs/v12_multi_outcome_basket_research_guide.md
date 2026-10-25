# Kalshi Arbitrage Engine — Multi-Outcome MECE Basket Research Guide

## 1. Overview

The Multi-Outcome Research Integration extends the Kalshi Arbitrage Engine's prospective evaluation pipeline from single-market binary complement arbitrage (`YES` + `NO` parity) to multi-contract event baskets.

In a mutually exclusive and collectively exhaustive (MECE) event, exactly one outcome will resolve to 100 cents (\$1.00) while all other outcomes resolve to 0 cents. If a trader purchases 1 contract of every outcome leg in the basket at a combined total cost strictly less than 100 cents (including exchange fees), the basket captures a guaranteed risk-free gross arbitrage.

### Core Safety Boundary
- **Read-Only / Simulation Only**: The engine strictly queries public, unauthenticated read-only market data endpoints.
- **Zero Live Execution**: No API keys or trading credentials are required or supported for live execution. All execution logic runs through simulated paper execution with strict double-entry ledger accounting.
- **Explicit Declaration Required**: To prevent invalid economic assumptions, the engine **never** infers MECE properties automatically. All outcome baskets must be explicitly declared and validated.

---

## 2. Declared MECE Basket Configuration

Baskets are configured using JSON arrays adhering to the `DeclaredMeceBasket` schema.

### Schema & Example
```json
[
  {
    "basket_id": "FED-RATE-DEC27",
    "event_ticker": "KXFEDDECISION-27DEC",
    "outcomes": [
      "CUT_MORE_THAN_25",
      "CUT_25",
      "MAINTAIN",
      "HIKE_25",
      "HIKE_MORE_THAN_25"
    ],
    "market_outcome_map": {
      "KXFEDDECISION-27DEC-C26": "CUT_MORE_THAN_25",
      "KXFEDDECISION-27DEC-C25": "CUT_25",
      "KXFEDDECISION-27DEC-H0": "MAINTAIN",
      "KXFEDDECISION-27DEC-H25": "HIKE_25",
      "KXFEDDECISION-27DEC-H26": "HIKE_MORE_THAN_25"
    },
    "basket_side": "yes",
    "notes": "December 2027 Fed Rate Decision 5-outcome mutually exclusive complete basket"
  }
]
```

### Validation Invariants
The loader [`load_declared_baskets()`](../kalshi_arbitrage/observer.py) and [`DeclaredMeceBasket`](../kalshi_arbitrage/observer.py) enforce:
1. `basket_id` and `event_ticker` must be non-empty strings.
2. `outcomes` must contain at least 2 distinct outcome names.
3. `market_outcome_map` must map each contract ticker to exactly one declared outcome.
4. **Bijective Completeness**: The set of mapped outcomes must exactly equal the set of declared outcomes (`set(market_outcome_map.values()) == set(outcomes)`). Duplicate outcome mappings or missing outcome legs are strictly rejected with `ValueError`.

---

## 3. Evaluation Funnel & Synchronization Rules

When declared baskets are configured, the prospective evaluator [`V11ProspectiveEvaluator`](../kalshi_arbitrage/v11_collector.py) executes a rigorous multi-stage validation funnel on each polling cycle:

```
[Polling Cycle Snapshots]
           │
           ▼
[Cycle-Scoped Leg Gathering] ── Missing Leg? ──► REJECTED: missing_market_data
           │
           ▼
[Staleness & Error Check] ── Stale/Error? ────► REJECTED: stale_market_data
           │
           ▼
[Timestamp Skew Check] ───── Skew > Limit? ───► REJECTED: excessive_leg_skew
           │
           ▼
[Depth Traversal Check] ──── Depth = 0? ──────► REJECTED: insufficient_depth
           │
           ▼
[Gross Parity Check] ─────── Cost >= 100¢? ───► REJECTED: negative_gross_edge
           │
           ▼
[CFTC Fee Evaluation] ────── Net Edge <= 0? ──► REJECTED: unprofitable_after_fees
           │
           ▼
[Risk Controls Check] ────── Limit Breached? ─► REJECTED: risk_limit_exceeded
           │
           ▼
[QUALIFIED & SIMULATED EXECUTION] ─────────────► Recorded to opportunities.jsonl
```

### Timestamp Skew & Synchronization
To prevent false arbitrage signals resulting from market movements between asynchronous HTTP responses:
- Leg snapshots must originate within the same collection cycle. Prior cycle snapshots are never mixed with current cycle observations.
- The maximum time delta between any two legs in the basket must satisfy:
  $$\Delta t_{\text{skew}} = \max(t_i) - \min(t_i) \le \text{max\_leg\_timestamp\_skew\_seconds}$$
  By default, `max_leg_timestamp_skew_seconds = 5.0` seconds. If exceeded, the opportunity is rejected with status `excessive_leg_skew`.

### Common Fill Quantity
Multi-outcome arbitrage requires simultaneous execution across all legs in equal quantities. The order-book depth analyzer inspects depth across all legs up to available liquidity or target limits, computing the common bottleneck fillable quantity $Q_{\text{common}} = \min(Q_1, Q_2, \ldots, Q_k)$. If any leg has zero fillable depth, the evaluation is rejected with `insufficient_depth`.

---

## 4. CLI Usage

The V11 collector CLI [`scripts/collect_v11.py`](../scripts/collect_v11.py) accepts declared baskets via file or inline JSON.

### Running with a Basket File
```bash
./.venv/bin/python scripts/collect_v11.py \
  --collect \
  --output-dir evidence_basket_run \
  --basket-file path/to/declared_baskets.json \
  --max-leg-skew 3.0 \
  --max-cycles 10 \
  --poll-interval 2.0
```

### Running with Inline JSON
```bash
./.venv/bin/python scripts/collect_v11.py \
  --collect \
  --output-dir evidence_basket_run \
  --basket-json '[{"basket_id":"FED-DEC","event_ticker":"KXFEDDECISION-27DEC","outcomes":["A","B"],"market_outcome_map":{"KXFEDDECISION-27DEC-A":"A","KXFEDDECISION-27DEC-B":"B"}}]' \
  --max-cycles 5
```

### CLI Options Reference
- `--basket-file PATH`: Path to a JSON file containing an array of declared MECE basket definitions. Automatically enables prospective evaluation and subscribes all constituent leg tickers.
- `--basket-json JSON_STRING`: Inline JSON string defining declared baskets.
- `--max-leg-skew SECONDS`: Maximum allowable timestamp skew between basket legs in seconds (default: `5.0`).
- `--tickers TICKER [TICKER ...]`: Optional additional standalone tickers to poll in parallel.

---

## 5. Output Streams and Cryptographic Manifest

When basket evaluation is enabled, the collector produces:
1. `observations.jsonl`: Individual market observation snapshots.
2. `diagnostics.jsonl`: Non-fatal transport errors and health telemetry.
3. `opportunities.jsonl`: Full stream of all evaluated basket opportunities (both qualified trades and structured rejections).
4. `manifest.json`: Cryptographic session manifest including:
   - `observations_sha256`: SHA-256 digest of observations.
   - `opportunities_sha256`: SHA-256 digest of evaluated opportunities.
   - `basket_evaluations_count`: Total number of basket evaluation passes executed.
   - `baskets_configured`: List of basket IDs monitored.
