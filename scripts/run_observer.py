#!/usr/bin/env python3
"""Read-only CLI entry point for the V5 Kalshi Market Observer.

Continuously polls public market data and order books, detects arbitrage
candidates, evaluates executable depth, persists timestamped evidence,
and produces an auditable summary report on exit.

Safety: Operates strictly with read-only public GET endpoints. No live orders
or private credentials are used or supported.
"""

import argparse
from pathlib import Path
import signal
import sys
from typing import List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalshi_arbitrage import (
    EvidenceStore,
    KalshiRestClient,
    MarketObserver,
    MarketObserverConfig,
    PaperPortfolio,
    RiskConfig,
    RiskManager,
)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Kalshi Arbitrage Engine V5 Market Observer (Read-Only)."
    )
    parser.add_argument(
        "--environment",
        choices=["demo", "production"],
        default="demo",
        help="Target Kalshi environment for public market data (default: demo).",
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=[],
        help="Specific market tickers to poll. If omitted, discovers open markets.",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=5,
        help="Maximum polling cycles to run before clean exit (default: 5).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Polling interval between cycles in seconds (default: 2.0).",
    )
    parser.add_argument(
        "--paper-trading",
        action="store_true",
        default=False,
        help="Enable V4 simulated paper trading for qualified opportunities (default: false).",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        help="Directory to persist timestamped observations and opportunities.",
    )
    parser.add_argument(
        "--fee-cents",
        type=int,
        default=1,
        help="Estimated fee per contract in cents for net profitability check (default: 1).",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=None,
        help="Maximum open markets to discover and poll per cycle (default: 20 in discovery mode; in explicit --tickers mode, all tickers are polled unless bounded).",
    )
    parser.add_argument(
        "--discovery-refresh-cycles",
        type=int,
        default=20,
        help="Cycles between refreshing open market discovery in discovery mode (default: 20).",
    )
    return parser.parse_args(argv)


def main(argv: List[str] = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    # Validate interaction between explicit --tickers and --max-markets
    if args.tickers:
        if args.max_markets is not None:
            if len(args.tickers) > args.max_markets:
                print(
                    f"Error: {len(args.tickers)} tickers were explicitly specified, but "
                    f"--max-markets was set to {args.max_markets}. Either remove --max-markets "
                    f"or increase it to at least {len(args.tickers)}.",
                    file=sys.stderr,
                )
                return 2
            poll_limit = args.max_markets
        else:
            # Honor all explicitly specified tickers without default truncation
            poll_limit = len(args.tickers)
        discovery_limit = poll_limit
    else:
        # Discovery mode: default to 20 if --max-markets omitted
        discovery_limit = args.max_markets if args.max_markets is not None else 20
        poll_limit = discovery_limit

    print("=" * 64)
    print("KALSHI ARBITRAGE ENGINE — V5 LIVE MARKET OBSERVER (READ-ONLY)")
    print(f"Environment:      {args.environment}")
    print(f"Polling Interval: {args.interval}s")
    print(f"Max Cycles:       {args.cycles}")
    if args.tickers:
        print(f"Target Tickers:   {', '.join(args.tickers)} ({len(args.tickers)} markets)")
        print(f"Polling Limit:    {poll_limit} markets")
    else:
        print(f"Discovery Limit:  {discovery_limit} markets")
        print(f"Discovery Refresh:{args.discovery_refresh_cycles} cycles")
    print(f"Paper Trading:    {'ENABLED (Simulation Only)' if args.paper_trading else 'DISABLED'}")
    print("=" * 64)

    client = KalshiRestClient(environment=args.environment)
    config = MarketObserverConfig(
        poll_interval_seconds=args.interval,
        default_fee_per_contract_cents=args.fee_cents,
        enable_paper_trading=args.paper_trading,
        max_cycles=args.cycles,
        max_discovered_markets=discovery_limit,
        max_poll_markets_per_cycle=poll_limit,
        discovery_refresh_interval_cycles=args.discovery_refresh_cycles,
    )

    evidence_store = EvidenceStore(args.evidence_dir) if args.evidence_dir else None

    portfolio = None
    risk_manager = None
    if args.paper_trading:
        portfolio = PaperPortfolio(initial_cash_cents=100_000)
        risk_manager = RiskManager(RiskConfig())

    observer = MarketObserver(
        client=client,
        config=config,
        target_tickers=args.tickers,
        evidence_store=evidence_store,
        paper_portfolio=portfolio,
        risk_manager=risk_manager,
    )

    sigint_count = 0

    def handle_sigint(signum, frame):
        nonlocal sigint_count
        sigint_count += 1
        if sigint_count == 1:
            print("\n[SIGINT received] Initiating clean observer shutdown... (press again to force exit)", flush=True)
            observer.stop()
        else:
            print("\n[SIGINT received] Force exiting immediately.", flush=True)
            sys.exit(130)

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        observer.run()
    except KeyboardInterrupt:
        print("\n[Interrupted] Cleanly exiting observer...", flush=True)
    except Exception as exc:
        print(f"\nObserver encountered an error: {exc}", file=sys.stderr)
        return 1

    report = observer.get_report()
    print("\n" + report.summary())
    return 0



if __name__ == "__main__":
    sys.exit(main())
