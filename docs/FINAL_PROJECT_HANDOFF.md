# Kalshi Arbitrage Engine — Final Project Handoff & Release Integrity Audit

**Document Version:** 1.0.0  
**Audit Date:** 2026-10-27  
**Audited Release Tag:** `v19.0.1`  
**Base Release Commit:** `21188a3f42c0382c5e19c8af96521afd46e9f7b1`  
**GitHub Repository:** [https://github.com/vikramaditya7z/engine-kal-t-1](https://github.com/vikramaditya7z/engine-kal-t-1)  
**Local Project Directory:** `/Users/vikramaditya/Disk/Projects/Kalshi`  
**Status:** **COMPLETE FOR DOCUMENTED RESEARCH SCOPE (ZERO BLOCKERS)**  

---

## 1. Project Purpose & Scope

The **Kalshi Arbitrage Engine** is a rigorous, deterministic quantitative research platform and market-surveillance system designed to analyze market microstructure, cross-contract pricing relationships, and the empirical presence or absence of arbitrage in Kalshi prediction markets.

### Primary Research Question
Does executable, risk-free arbitrage exist across mutually exclusive, collectively exhaustive (MECE) binary prediction market contracts on Kalshi after accounting for full order-book depth, volume-weighted average price (VWAP), regulatory CFTC taker fees, execution latency, and inter-leg observation skew?

### Scope Boundaries
- **Research & Surveillance:** Ingestion, validation, reconstruction, economic attribution, and paper evaluation.
- **Zero Real-Money Execution:** Strictly read-only; contains zero logic, routes, or credentials for placing live orders or handling exchange accounts.
- **Strict Invariants:** Mathematical proofs and exact integer-cent double-entry accounting ($1.00 = 100¢).

---

## 2. Current Published Release & Repository State

- **Published Release Tag:** `v19.0.1` (annotated tag `e5a127de3764fdf2bf146ae33eb6aee8a9e6c8a3`)
- **Published Target Commit:** `21188a3f42c0382c5e19c8af96521afd46e9f7b1`
- **Remote Branches:**
  - `origin/main` -> `73afc04513019fae421e6f5d489c06daa103246f` (tag: `v19.0`)
  - `origin/fix/v19-clean-checkout-test` -> `21188a3f42c0382c5e19c8af96521afd46e9f7b1` (tag: `v19.0.1`)
- **Local Working Tree Status:**
  - On branch `fix/v19-clean-checkout-test`, up to date with `origin/fix/v19-clean-checkout-test`.
  - Unstaged modification: `tests/fixtures/kalshi_market_response.json` (auto-updated by public market-data smoke test).
  - Untracked artifacts: Local historical research trial directories (`evidence_v12_fed_basket_trial/`, `evidence_v14_multi_basket_trial/`, `evidence_v16_streaming_trial/`, `evidence_v17_broad_market_trial/`, `evidence_v18_economics_trial/`), local reproduction run outputs (`data/v*/`), and post-release documentation reports (`docs/V19_0_1_FULL_SYSTEM_QA_REPORT.md`, `docs/V19_0_1_PRODUCT_VERIFICATION_REPORT.md`).
  - **Integrity Rule Adherence:** No destructive commands (`git reset`, `git clean`, `git stash`) were executed. Working tree is preserved as-is.

---

## 3. Verified Architecture & Component Inventory

```
kalshi_arbitrage/
├── __init__.py                  # Public API exports and version metadata
├── attribution.py               # Two-sided (Long YES/NO) economic attribution & complementarity theorem
├── contract.py                  # Immutable BinaryContract and Side abstractions
├── coordinator.py              # Single-process coordination and lifecycle management
├── dashboard/                   # Built-in read-only HTTP Web Terminal
│   ├── server.py                # Python http.server.HTTPServer & REST routing
│   ├── service.py               # Aggregation service discovering workspace datasets
│   └── static/
│       ├── index.html           # Semantic HTML5 single-page terminal UI
│       ├── dashboard.css        # Responsive dark-mode styling
│       └── dashboard.js         # Vanilla ES6 client, auto-refresh polling loop, CDP testable
├── discovery.py                 # BroadMarketDiscoveryEngine with 7-tier MECE taxonomy
├── episode_tracker.py           # Cryptographic fingerprinting for opportunity episodes
├── evaluation.py                # Multi-stage qualification funnel & historical evaluator
├── evidence_storage.py          # Atomic JSONL observation storage & SHA-256 manifests
├── execution.py                 # Multi-level order book depth traversal & VWAP pricing
├── fee_model.py                 # CFTC-regulated quadratic taker and maker fee calculation
├── ledger.py                    # Double-entry portfolio ledger with integer-cent invariants
├── market_data.py               # OrderBook, Level, and Market data normalization
├── observer.py                  # MarketObserver, DeclaredMeceBasket, MarketObservation
├── pricing.py                   # Integer-cent math and parity evaluation
├── rest_client.py               # Bounded, read-only public REST client (urllib GET transport)
├── risk.py                      # Pre-trade risk controls and paper position limits
├── snapshot_reconstruction.py   # Deterministic multi-leg cycle matching & skew filtering
├── universe.py                  # Liquid universe selection & filter criteria
└── ws_streaming.py              # WebSocket v2 message framing & simulated L2 order book sync

scripts/
├── run_dashboard.py             # HTTP dashboard server runner (default port: 8080)
├── smoke_test_markets.py        # Live unauthenticated public market-data smoke test
├── evaluate_v10.py              # Historical strategy replay CLI
├── collect_v11.py               # Bounded live prospective collection CLI
├── collect_v16.py               # Synchronized REST collection & comparison CLI
├── collect_v17.py               # Broad-market discovery & prospective monitoring CLI
└── evaluate_v18.py              # Two-sided multi-universe economic attribution CLI
```

---

## 4. Setup & Dashboard Launch Commands

### 4.1 Environment Initialization
```bash
# 1. Clone repository
git clone https://github.com/vikramaditya7z/engine-kal-t-1.git
cd engine-kal-t-1

# 2. Checkout v19.0.1 release tag
git checkout v19.0.1

# 3. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 4. Install project in editable mode with development dependencies
pip install --upgrade pip
pip install -e ".[dev]"
```

### 4.2 Starting and Stopping the Operations Dashboard
```bash
# Start the read-only dashboard server (default binding: http://127.0.0.1:8080)
python scripts/run_dashboard.py --port 8080

# Or customize host and port:
python scripts/run_dashboard.py --host 0.0.0.0 --port 8888

# Access the interface in your browser:
# http://127.0.0.1:8080

# Safe Stop:
# Press Ctrl+C in the terminal (SIGINT is trapped, triggering clean server shutdown)
```

---

## 5. Testing & Research Workflow Commands

### 5.1 Automated Test Suites
```bash
# Run full automated test suite (628 tests in local repository; 626 tests + 2 graceful skips in clean clone)
pytest -v

# Run fast quiet verification
pytest -q

# Run specific milestone tests
pytest tests/test_v18_economics.py tests/test_v19_release_gates.py -v
```

### 5.2 Public Market-Data Smoke Test
```bash
# Unauthenticated GET request verifying public API connectivity and JSON schema
python scripts/smoke_test_markets.py
```

### 5.3 Historical Replay
```bash
# Replay prospective observations through deterministic reconstructor, evaluator, and ledger
python scripts/evaluate_v10.py --evidence-dir evidence_dashboard_trial
```

### 5.4 Bounded Synchronized Collection
```bash
# Run 5-cycle concurrent REST collection trial with inter-leg skew analysis
python scripts/collect_v16.py \
  --output-dir /tmp/v16_trial_run \
  --cycles 5 \
  --cycle-interval 2.0
```

### 5.5 Broad-Market Discovery & Prospective Monitoring
```bash
# Scan catalog, classify MECE structures, and monitor candidate events
python scripts/collect_v17.py \
  --output-dir /tmp/v17_trial_run \
  --cycles 5 \
  --cycle-interval 2.0 \
  --max-events-to-scan 50 \
  --max-selected-events 3
```

### 5.6 Two-Sided Multi-Universe Economic Attribution
```bash
# Run two-sided attribution analysis across distinct economic cohorts
python scripts/evaluate_v18.py \
  --output-dir /tmp/v18_trial_run \
  --cycles 2 \
  --cycle-interval 1.0 \
  --concurrency 4
```

---

## 6. Verified Test Results & Environment Reproducibility

| Environment | Test Command | Passed | Skipped | Failed | Duration | Verification Notes |
|---|---|---|---|---|---|---|
| **Local Checkout** (`/Users/vikramaditya/Disk/Projects/Kalshi`) | `./.venv/bin/pytest -v` | **628** | 0 | 0 | 16.40s | Includes real-evidence tests running against untracked trial folders |
| **Fresh Clone** (`/tmp/kalshi_fresh_clone_audit`, isolated venv) | `pytest -v` | **626** | **2** | 0 | 15.84s | 100% pass on all unit, integration, simulation, and gate tests |

### Clarification on the 2 Skipped Tests in Clean Checkout
1. `tests/test_v13_basket_replay.py:681` (`test_replay_real_v12_trial_evidence_exact_matching`):
   - **Reason:** Large raw research directory `evidence_v12_fed_basket_trial/` was excluded from git tracking to keep repository lightweight. Test contains an intentional guard: `pytest.skip("V12 trial evidence not present...")`.
2. `tests/test_v18_economics.py:263` (`test_v17_recorded_evidence_economic_attribution`):
   - **Reason:** Large raw directory `evidence_v17_broad_market_trial/` was excluded from git tracking. Test contains an intentional guard: `pytest.skip("V17 evidence file not present")`.
- When these local trial directories are present (as in the developer workspace), both tests execute and pass, yielding 628 passed.

---

## 7. Principal Research Evidence Inventory

All research evidence files adhere to standard JSON Lines (`.jsonl`) schema with accompanied `manifest.json` cryptographic hashes:

| Directory | Records | Primary Research Scope | Key Findings |
|---|---|---|---|
| `evidence_dashboard_trial/` | 44 | Dashboard integration and visual test baseline | Tracked in Git for instant out-of-the-box UI demo |
| `evidence_v11_prospective_trial/` | 10 | Bounded prospective collection validation | Validated point-in-time sequential ingestion |
| `evidence_v12_fed_basket_trial/` | 50 | 5-leg FOMC monetary policy rate partition | 10 reconstructed snapshots; -2¢ gross edge |
| `evidence_v14_multi_basket_trial/` | 50 | Multi-event concurrent prospective monitoring | Confirmed cross-event isolation |
| `evidence_v16_streaming_trial/` | 100 | Sequential vs concurrent REST latency comparison | Thread pool reduced inter-leg skew from 9.0s to 0.0s |
| `evidence_v17_broad_market_trial/` | 300 | Broad-market universe scan (30 markets, 3 events) | 30 reconstructed snapshots; -31¢ to -8¢ gross edge |
| `evidence_v18_economics_trial/` | 90 | Two-sided (Long YES/NO) economic attribution | Proved Complementarity Theorem; 100% negative edges |

---

## 8. Engine Capabilities: What It Can and Cannot Do

### What the Engine CAN Do:
- **Public Ingestion:** Ingest public market data, L2 books, and event hierarchies without credentials.
- **MECE Taxonomy:** Classify multi-market events into verified partition structures using 7 deterministic heuristic rules.
- **Microsecond Synchronization:** Reconstruct simultaneous multi-leg order book snapshots and discard those violating timestamp skew bounds.
- **Complete Liquidity Traversal:** Consume multiple price levels of order books and compute true VWAP and limiting leg bottleneck capacity.
- **Regulatory Fee Modeling:** Compute exact CFTC quadratic taker/maker fees in integer cents.
- **Economic Attribution:** Categorize snapshot rejections into Normal Market-Maker Over-Round, Fee Erosion, Insufficient Depth, or Leg Timestamp Skew.
- **Paper Execution:** Simulate double-entry cash and position accounting with solvency reconciliation.
- **Visual Analytics:** Provide a local, zero-dependency dark-mode web terminal for real-time and historical inspection.

### What the Engine CANNOT Do:
- **Cannot Route Live Orders:** Contains zero API routes for `POST /portfolio/orders` or any order entry.
- **Cannot Manage Capital:** Has no private wallet or banking connection.
- **Cannot Guarantee Profitability:** Tested continuous resting limit order books consistently show negative gross and net edge after spreads and fees.
- **Cannot Connect Live WebSocket Without Keys:** Kalshi requires RSA signatures for WebSocket connection, which is intentionally unconfigured to preserve the read-only security boundary.

---

## 9. Known Limitations & Unverified Behaviors

1. **Discrete Temporal Sampling:** The empirical findings reflect discrete polling intervals during continuous trading hours. Ephemeral, sub-millisecond price dislocations occurring during breaking news events were not observed.
2. **Sequential Multi-Leg Execution ("Legging Out"):** If an ephemeral mispricing exists, crossing multiple books sequentially exposes the trader to adverse selection and partial fill execution risk.
3. **Simulated vs Authenticated WebSocket:** The WebSocket v2 client protocol parser is verified through hermetic unit test fixtures (`test_v16_streaming.py`), but has not been connected to live streaming with private keys.

---

## 10. Step-by-Step Developer Quickstart

To reproduce the entire project from scratch in under 5 minutes:

```bash
# 1. Clone fresh repository
git clone https://github.com/vikramaditya7z/engine-kal-t-1.git
cd engine-kal-t-1
git checkout v19.0.1

# 2. Set up environment
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"

# 3. Verify test suite (626 passing, 2 intentional skips)
pytest -v

# 4. Launch Operations Dashboard
python scripts/run_dashboard.py --port 8080

# 5. Open browser at http://127.0.0.1:8080
# Select 'evidence_dashboard_trial' from the dropdown to view metrics, books, and audit views.
```

---

## 11. Final Status & Sign-Off

**Status:** **COMPLETE FOR DOCUMENTED RESEARCH SCOPE**  
**Blockers:** **NONE**  

All planned engineering milestones (V0 through V19) are verified, tested, documented, and published. The project stands as a fully operational, scientifically rigorous quantitative research engine.
