#!/usr/bin/env python3
"""CLI Entry point for the Kalshi Arbitrage Engine V9 Autonomous Paper Trader.

Usage:
    # Run a bounded 10-cycle paper trading session
    python scripts/run_paper_trader.py --bounded-cycles 10 --poll-interval 2.0

    # Run continuous session with custom state persistence
    python scripts/run_paper_trader.py --state-file data/my_paper_session.json

    # Emergency halt test with kill-switch
    python scripts/run_paper_trader.py --kill-switch

All executions are strictly simulated in paper mode. Live order placement is prohibited.
"""

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import signal
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.coordinator import (
    CoordinatorError,
    CoordinatorState,
    PaperCoordinatorConfig,
    PaperTradingCoordinator,
)
from kalshi_arbitrage.paper_risk import PaperRiskConfig

logger = logging.getLogger("paper_trader")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    format_str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=format_str, datefmt="%H:%M:%S")


def print_session_summary(report: dict) -> None:
    print("\n" + "=" * 70)
    print(" KALSHI ARBITRAGE ENGINE V9 — AUTONOMOUS PAPER TRADING SESSION SUMMARY")
    print("=" * 70)
    print(f" Session ID:           {report.get('session_id')}")
    print(f" Execution Mode:       {report.get('execution_mode', '').upper()} (Simulated)")
    print(f" Created At:           {report.get('created_at')}")
    print(f" Completed Cycles:     {report.get('total_cycles_completed')}")
    print("-" * 70)
    init_cash = report.get("initial_cash_cents", 0) / 100.0
    avail_cash = report.get("available_cash_cents", 0) / 100.0
    equity = report.get("book_value_equity_cents", 0) / 100.0
    cost_basis = report.get("total_cost_basis_cents", 0) / 100.0
    realized_pnl = report.get("realized_pnl_cents", 0) / 100.0
    fees = report.get("total_fees_cents", 0) / 100.0

    print(f" Initial Capital:      ${init_cash:,.2f}")
    print(f" Available Cash:       ${avail_cash:,.2f}")
    print(f" Position Cost Basis:  ${cost_basis:,.2f}")
    print(f" Total Book Equity:    ${equity:,.2f}")
    print(f" Realized P&L:         ${realized_pnl:+,.2f}")
    print(f" Cumulative Fees:      ${fees:,.2f}")
    print("-" * 70)
    print(f" Trades Attempted:     {report.get('total_trades_attempted')}")
    print(f" Arbitrage Complete:   {report.get('trades_completed_arbitrage')}")
    print(f" Unwound / Partial:    {report.get('trades_partial_or_unwound')}")
    print(f" Open Positions Count: {len(report.get('open_positions', []))}")
    print(f" Ledger Reconciled:    {'YES (100% Invariant Match)' if report.get('is_reconciled') else 'NO - AUDIT DISCREPANCY!'}")
    print(f" Kill-Switch Active:   {report.get('kill_switch_active')}")
    if report.get("kill_switch_active"):
        print(f" Kill-Switch Reason:   {report.get('kill_switch_reason')}")
    print("=" * 70 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Autonomous Paper Trading System for Kalshi Prediction Markets (Milestone V9)"
    )
    parser.add_argument(
        "--mode",
        choices=["paper"],
        default="paper",
        help="Execution mode (strictly 'paper'; live order placement is prohibited)",
    )
    parser.add_argument(
        "--bounded-cycles",
        "--max-cycles",
        dest="bounded_cycles",
        type=int,
        default=None,
        help="Run for a fixed number of cycles then terminate cleanly",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Polling delay in seconds between trading cycles (default: 5.0)",
    )
    parser.add_argument(
        "--discovery-interval",
        type=float,
        default=60.0,
        help="Interval in seconds between full market discovery refreshes (default: 60.0)",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=50,
        help="Maximum markets to evaluate per cycle (default: 50)",
    )
    parser.add_argument(
        "--initial-cash-cents",
        type=int,
        default=1_000_000,
        help="Initial capital in cents for fresh sessions (default: 1,000,000 = $10,000.00)",
    )
    parser.add_argument(
        "--state-file",
        type=str,
        default="data/paper_trading_state.json",
        help="Path to atomic persistent state JSON file",
    )
    parser.add_argument(
        "--clean-session",
        "--clean",
        dest="clean_session",
        action="store_true",
        help="Ignore existing state file and start a fresh session",
    )
    parser.add_argument(
        "--kill-switch",
        action="store_true",
        help="Start with global kill-switch engaged (blocks all new simulated orders)",
    )
    parser.add_argument(
        "--report-output",
        "--report-file",
        dest="report_output",
        type=str,
        default=None,
        help="Path to write final session summary JSON report",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose debug logging",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.mode != "paper":
        logger.critical("Only 'paper' mode is supported. Exiting for safety.")
        return 1

    risk_cfg = PaperRiskConfig()
    coord_cfg = PaperCoordinatorConfig(
        execution_mode="paper",
        state_filepath=Path(args.state_file),
        poll_interval_seconds=args.poll_interval,
        discovery_refresh_interval_seconds=args.discovery_interval,
        max_cycles=args.bounded_cycles,
        max_markets_per_cycle=args.max_markets,
        initial_cash_cents=args.initial_cash_cents,
        clean_session=args.clean_session,
        risk_config=risk_cfg,
    )

    coordinator = PaperTradingCoordinator(config=coord_cfg)

    # Intercept termination signals for graceful shutdown
    def sig_handler(signum, frame):
        logger.info(f"Signal {signum} caught. Initiating graceful coordinator shutdown...")
        coordinator.stop()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        coordinator.startup()

        if args.kill_switch:
            coordinator.risk_gate.activate_kill_switch("Engaged via CLI flag --kill-switch")

        coordinator.run_loop()

    except Exception as exc:
        logger.exception(f"Fatal error during coordinator execution: {exc}")
        return 1
    finally:
        coordinator.shutdown()
        report = coordinator.generate_session_report()
        print_session_summary(report)

        if args.report_output:
            out_path = Path(args.report_output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            logger.info(f"Session report saved to {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
