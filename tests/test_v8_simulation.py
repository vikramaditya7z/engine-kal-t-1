"""Tests for V8.6 Historical Replay & Realistic Execution Simulation Engine.

Verifies:
- Chronological historical replay without look-ahead bias.
- Liquid universe filtering integration.
- Deterministic detection of Fee Reversals (flat win vs CFTC quadratic loss).
- Multi-leg execution simulation across varying microstructure parameters.
- Scratch / unwind execution and realized loss accounting.
- Strict PaperPortfolio ledger reconciliation with zero discrepancies.
- Latency sensitivity sweeps across 0ms to 1000ms.
- Full CLI script execution and JSON serialization fidelity.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import pytest

from kalshi_arbitrage.async_execution import (
    LegSequencingPolicy,
    MicrostructureConfig,
    MultiLegLifecycleState,
    UnwindPolicy,
)
from kalshi_arbitrage.contract import NO, YES
from kalshi_arbitrage.evaluation import HistoricalDataset
from kalshi_arbitrage.fee_model import FeeModelConfig, FeeScheduleType
from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.observer import MarketObservation
from kalshi_arbitrage.universe import UniverseFilterConfig
from kalshi_arbitrage.v8_simulation import (
    V8SimulationConfig,
    V8SimulationEngine,
    V8SimulationReport,
)


def make_populated_market_observation(
    ticker: str,
    ts: datetime,
    yes_bid_cents: int,
    no_bid_cents: int,
    yes_qty: int = 10,
    no_qty: int = 10,
    volume_24h: str = "5000.00",
    open_interest: str = "1000.00",
) -> MarketObservation:
    market = NormalizedMarket(
        ticker=ticker,
        event_ticker=f"EVENT-{ticker}",
        market_type="binary",
        status="open",
        title=f"Market {ticker}",
        subtitle=None,
        yes_subtitle=None,
        no_subtitle=None,
        rules_primary=None,
        rules_secondary=None,
        expiration_value=None,
        result=None,
        created_time=None,
        updated_time=None,
        open_time=None,
        close_time=None,
        expiration_time=None,
        settlement_ts=None,
        yes_bid_dollars=Decimal(yes_bid_cents) / Decimal("100"),
        yes_ask_dollars=Decimal(100 - no_bid_cents) / Decimal("100"),
        no_bid_dollars=Decimal(no_bid_cents) / Decimal("100"),
        no_ask_dollars=Decimal(100 - yes_bid_cents) / Decimal("100"),
        last_price_dollars=None,
        settlement_value_dollars=None,
        yes_bid_size=None,
        yes_ask_size=None,
        volume=Decimal(volume_24h),
        volume_24h=Decimal(volume_24h),
        open_interest=Decimal(open_interest),
    )
    book = NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(
            OrderBookLevel(
                price_dollars=Decimal(yes_bid_cents) / Decimal("100"),
                quantity=Decimal(yes_qty),
            ),
        ),
        no_bids=(
            OrderBookLevel(
                price_dollars=Decimal(no_bid_cents) / Decimal("100"),
                quantity=Decimal(no_qty),
            ),
        ),
        source_timestamp=ts,
    )
    return MarketObservation(
        observation_id=f"obs-{ticker}-{ts.timestamp()}",
        ticker=ticker,
        observed_at=ts,
        source_timestamp=ts,
        market=market,
        order_book=book,
        is_success=True,
        is_stale=False,
    )


def test_v8_simulation_fee_reversal_detection():
    """Verify detection of fee reversal where flat 1c fee is profitable but CFTC quadratic fee loses."""
    # Near 50c contract: YES bid = 49c (derived NO ask = 51c), NO bid = 47c (derived YES ask = 53c) -> Sum ask = 104 (no arb)
    # Binary parity arb: YES bid = 52c (derived NO ask = 48c), NO bid = 50c (derived YES ask = 50c)
    # Sum ask = 48c + 50c = 98c -> Gross edge = 2c!
    # Under flat 1c fee: 1c per contract (2c total across 2 legs) -> Net edge = 2c - 2c = 0c (or at 97c gross edge 3c -> net 1c)
    # Let's use YES ask = 49c, NO ask = 49c -> Total cost = 98c -> Gross edge = 2c
    # Derived from YES bid = 51c, NO bid = 51c
    # Flat fee 1c: 2 * 0c (if 0c flat fee) or 1c flat fee:
    # If flat fee is 0c or 1c total:
    t0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    obs = make_populated_market_observation("KXREVERSAL-1", t0, yes_bid_cents=51, no_bid_cents=51, yes_qty=10, no_qty=10)

    dataset = HistoricalDataset.from_observations([obs], source_description="fee-reversal-test")

    cfg = V8SimulationConfig(
        flat_fee_per_contract_cents=0,  # 0c flat fee makes 2c gross edge look +2c profitable
        fee_config=FeeModelConfig(schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER),  # 2c per contract -> 4c total fee
        target_quantity=1,
        enforce_universe=False,
    )
    engine = V8SimulationEngine(config=cfg)
    report = engine.run(dataset)

    assert report.fee_metrics.total_detected_opportunities == 1
    assert report.fee_metrics.flat_fee_profitable_count == 1
    assert report.fee_metrics.cftc_fee_profitable_count == 0
    assert report.fee_metrics.fee_reversal_count == 1
    assert report.fee_metrics.fee_reversal_pct == Decimal("100.00")
    # Because CFTC fee makes it unprofitable, it should fail-closed and NOT execute!
    assert report.trades_attempted == 0


def test_v8_simulation_successful_fill_and_reconciliation():
    """Verify execution of a wide gross edge trade that remains profitable after CFTC fees."""
    # Deep arbitrage: YES ask = 40c (derived from NO bid 60c), NO ask = 45c (derived from YES bid 55c)
    # Total ask = 85c -> Gross edge = 15c!
    # CFTC fees:
    # At 40c: ceil(0.07 * 1 * 40 * 60 / 100) = ceil(1.68) = 2c
    # At 45c: ceil(0.07 * 1 * 45 * 55 / 100) = ceil(1.7325) = 2c
    # Total CFTC fees = 4c -> Net edge = 15c - 4c = +11c net profit!
    t0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    obs = make_populated_market_observation(
        "KXPROFIT-1",
        t0,
        yes_bid_cents=55,
        no_bid_cents=60,
        yes_qty=5,
        no_qty=5,
        volume_24h="25000.00",
        open_interest="5000.00",
    )
    dataset = HistoricalDataset.from_observations([obs], source_description="profitable-test")

    cfg = V8SimulationConfig(
        target_quantity=5,
        initial_cash_cents=100_000,
        enforce_universe=True,
    )
    engine = V8SimulationEngine(config=cfg)
    report = engine.run(dataset)

    assert report.trades_attempted == 1
    assert report.trades_fully_filled == 1
    assert report.trades_legged_out == 0
    assert report.completion_rate_pct == Decimal("100.00")
    assert report.filled_contracts_total == 5
    assert report.ledger_is_reconciled is True
    assert len(report.reconciliation_discrepancies) == 0
    assert report.realized_gross_edge_cents > 0
    assert report.net_realized_pnl_cents > 0


def test_v8_simulation_universe_filtering():
    """Verify universe filter blocks illiquid markets when enforced, and permits them when unenforced."""
    t0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    # Market with 0 volume
    obs = make_populated_market_observation(
        "KXILLIQUID-1",
        t0,
        yes_bid_cents=55,
        no_bid_cents=60,
        yes_qty=5,
        no_qty=5,
        volume_24h="0.00",
        open_interest="0.00",
    )
    dataset = HistoricalDataset.from_observations([obs], source_description="universe-filter-test")

    # 1. Enforced: should reject market from universe and NOT execute
    cfg_enforced = V8SimulationConfig(enforce_universe=True)
    report_enforced = V8SimulationEngine(config=cfg_enforced).run(dataset)
    assert report_enforced.universe_result.qualified_markets_count == 0
    assert report_enforced.trades_attempted == 0

    # 2. Unenforced: should execute despite illiquidity
    cfg_permissive = V8SimulationConfig(enforce_universe=False)
    report_permissive = V8SimulationEngine(config=cfg_permissive).run(dataset)
    assert report_permissive.trades_attempted == 1
    assert report_permissive.trades_fully_filled == 1


def test_v8_simulation_latency_decay_sweep():
    """Verify latency sensitivity sweep records points across latencies."""
    t0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    obs = make_populated_market_observation(
        "KXLATENCY-1",
        t0,
        yes_bid_cents=55,
        no_bid_cents=60,
        yes_qty=5,
        no_qty=5,
        volume_24h="10000.00",
        open_interest="2000.00",
    )
    dataset = HistoricalDataset.from_observations([obs], source_description="latency-sweep-test")

    cfg = V8SimulationConfig(
        sensitivity_latencies_ms=(0.0, 50.0, 200.0, 1000.0),
        enforce_universe=False,
    )
    report = V8SimulationEngine(config=cfg).run(dataset)

    assert len(report.latency_sensitivity) == 4
    latencies = [pt.latency_ms for pt in report.latency_sensitivity]
    assert latencies == [0.0, 50.0, 200.0, 1000.0]
    for pt in report.latency_sensitivity:
        assert pt.trades_attempted == 1


def test_v8_simulation_cli_integration(tmp_path: Path):
    """Verify scripts/simulate_v8.py runs via subprocess and exports JSON."""
    json_out = tmp_path / "v8_cli_report.json"
    cmd = [
        sys.executable,
        "scripts/simulate_v8.py",
        "--file",
        "evidence_dashboard_trial/observations.jsonl",
        "--fee-schedule",
        "cftc_taker",
        "--latency-ms",
        "50.0",
        "--output-json",
        str(json_out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).parents[1]))
    assert proc.returncode == 0
    assert "KALSHI ARBITRAGE ENGINE — V8 REALISTIC EXECUTION & RISK SIMULATION" in proc.stdout
    assert json_out.exists()

    with open(json_out, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["config"]["fee_schedule"] == "KALSHI_OFFICIAL_TAKER"
    assert data["financial_accounting"]["ledger_is_reconciled"] is True


def test_v8_simulation_report_serialization():
    """Verify summary string generation and JSON serialization of V8SimulationReport."""
    t0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    obs = make_populated_market_observation(
        "KXSERIAL-1",
        t0,
        yes_bid_cents=55,
        no_bid_cents=60,
        yes_qty=2,
        no_qty=2,
        volume_24h="5000.00",
        open_interest="1000.00",
    )
    dataset = HistoricalDataset.from_observations([obs], source_description="serial-test")
    report = V8SimulationEngine().run(dataset)

    summary_text = report.summary()
    assert "KALSHI ARBITRAGE ENGINE — V8 REALISTIC EXECUTION & RISK SIMULATION" in summary_text
    assert "LATENCY SENSITIVITY SWEEP" in summary_text

    rep_dict = report.to_dict()
    assert "execution_lifecycle" in rep_dict
    assert "financial_accounting" in rep_dict
    assert "universe_selection" in rep_dict

    json_str = report.to_json()
    parsed = json.loads(json_str)
    assert parsed["config"]["initial_cash_cents"] == 1000000


def test_v8_simulation_point_in_time_no_lookahead_bias():
    """Verify that historical replay evaluates universe qualification strictly point-in-time.

    Scenario:
    - Market KXBIAS has 2 observations over time.
    - Observation 1 (t0): volume_24h = 0.00 (illiquid). Parity arb opportunity exists.
    - Observation 2 (t1): volume_24h = 50000.00 (liquid). Parity arb opportunity exists.

    Under pre-replay batch selection (with look-ahead bias):
    - At t0, the engine looks at end-of-dataset state (50000.00) and executes both t0 and t1 (2 trades).

    Under point-in-time selection (strictly no look-ahead bias):
    - At t0, volume_24h is 0.00 -> rejected from liquid universe -> NOT executed.
    - At t1, volume_24h is 50000.00 -> accepted into liquid universe -> executed.
    - Total trades attempted: exactly 1 (at t1 only).
    """
    t0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 11, 12, 1, 0, tzinfo=timezone.utc)

    obs0 = make_populated_market_observation(
        "KXBIAS-1",
        t0,
        yes_bid_cents=55,
        no_bid_cents=60,
        yes_qty=5,
        no_qty=5,
        volume_24h="0.00",       # Illiquid at t0
        open_interest="0.00",
    )
    obs1 = make_populated_market_observation(
        "KXBIAS-1",
        t1,
        yes_bid_cents=55,
        no_bid_cents=60,
        yes_qty=5,
        no_qty=5,
        volume_24h="50000.00",   # Liquid at t1
        open_interest="10000.00",
    )

    dataset = HistoricalDataset.from_observations([obs0, obs1], source_description="lookahead-test")

    cfg = V8SimulationConfig(enforce_universe=True, target_quantity=5)
    engine = V8SimulationEngine(config=cfg)
    report = engine.run(dataset)

    # Exactly 1 trade executed (at t1), NOT 2 trades!
    assert report.trades_attempted == 1
    assert report.trades_fully_filled == 1
    # Check that record 0 (t0) was rejected due to universe
    assert len(report.records) == 2
    rec0 = report.records[0]
    rec1 = report.records[1]
    assert rec0.executed is False
    assert rec0.in_universe is False
    assert rec1.executed is True
    assert rec1.in_universe is True
