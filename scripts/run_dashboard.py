#!/usr/bin/env python3
"""CLI entry point for the Kalshi Arbitrage Engine Dashboard (Read-Only Frontend V1).

Serves the quantitative trading terminal UI and read-only REST APIs over HTTP.
Requires no external web dependencies.
"""

import argparse
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalshi_arbitrage.dashboard import create_dashboard_server


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Kalshi Arbitrage Engine Dashboard (Read-Only Web Terminal)."
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host address to bind to (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080; falls back to next available port if in use).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("=" * 64)
    print("KALSHI ARBITRAGE ENGINE — FRONTEND DASHBOARD V1 (READ-ONLY)")
    print(f"Binding Address: http://{args.host}:{args.port}")
    print("=" * 64)

    server = create_dashboard_server(
        host=args.host,
        port=args.port,
        workspace_root=ROOT,
    )

    def handle_signal(signum, frame):
        print("\n[Shutdown] Shutting down dashboard server...", flush=True)
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    url = server.start(blocking=False)
    print(f"\nDashboard is live and ready:")
    print(f"  --> {url}\n")
    print("Press Ctrl+C to stop the dashboard server.\n")

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        handle_signal(signal.SIGINT, None)

    return 0


if __name__ == "__main__":
    sys.exit(main())
