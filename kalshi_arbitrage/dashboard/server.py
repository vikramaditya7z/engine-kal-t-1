"""HTTP Server and request handler for the Kalshi Arbitrage Dashboard.

Built strictly using standard library http.server, requiring zero third-party web frameworks.
Exposes read-only REST endpoints and serves static UI assets.
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import logging
from pathlib import Path
import threading
from typing import Optional, Tuple, Union
from urllib.parse import parse_qs, urlparse

from .service import DashboardDataService

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Custom HTTP handler serving read-only REST endpoints and static files."""

    service = DashboardDataService()

    def do_GET(self) -> None:
        """Handle incoming GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Static assets
        if path in ("", "/", "/index.html"):
            self._serve_static_file("index.html", "text/html; charset=utf-8")
            return
        elif path == "/dashboard.css":
            self._serve_static_file("dashboard.css", "text/css; charset=utf-8")
            return
        elif path == "/dashboard.js":
            self._serve_static_file("dashboard.js", "application/javascript; charset=utf-8")
            return

        # API Endpoints
        dataset_id = query.get("dataset", [None])[0]

        if path in ("/api/status", "/api/health", "/api/system/health"):
            health = self.service.get_system_health()
            self._send_json(200, health)
            return

        elif path == "/api/overview":
            overview = self.service.get_overview(dataset_id)
            self._send_json(200, overview)
            return

        elif path == "/api/markets":
            markets = self.service.get_markets_table(dataset_id)
            meta = self.service.get_markets_metadata(dataset_id)
            resp = {"markets": markets, "count": len(markets)}
            resp.update(meta)
            self._send_json(200, resp)
            return

        elif path == "/api/opportunities":
            simulated = query.get("simulated", ["false"])[0].lower() in ("true", "1", "yes")
            opps = self.service.get_opportunities(dataset_id, include_simulated=simulated)
            self._send_json(200, opps)
            return

        elif path == "/api/historical":
            hist = self.service.get_historical_validation(dataset_id)
            self._send_json(200, hist)
            return

        elif path == "/api/v8/simulation":
            latency_str = query.get("latency_ms", ["50.0"])[0]
            try:
                lat = float(latency_str)
            except ValueError:
                lat = 50.0
            enforce = query.get("enforce_universe", ["true"])[0].lower() in ("true", "1", "yes")
            v8_sim = self.service.get_v8_simulation(
                dataset_id,
                latency_ms=lat,
                enforce_universe=enforce,
            )
            self._send_json(200, v8_sim)
            return

        elif path in ("/api/paper", "/api/paper/status"):
            paper_status = self.service.get_paper_trading_status()
            self._send_json(200, paper_status)
            return

        elif path in ("/api/collector", "/api/collector/status"):
            collector_status = self.service.get_continuous_collector_status()
            self._send_json(200, collector_status)
            return

        elif path in ("/api/evaluator", "/api/evaluator/status"):
            evaluator_status = self.service.get_continuous_evaluator_status()
            self._send_json(200, evaluator_status)
            return

        elif path in ("/api/live/summary", "/api/pipeline/summary"):
            summary = self.service.get_live_pipeline_summary()
            self._send_json(200, summary)
            return

        elif path in ("/api/evaluator/candidates", "/api/candidates"):
            strategy = query.get("strategy", [None])[0]
            outcome = query.get("outcome", [None])[0]
            state = query.get("state", [None])[0]
            market = query.get("market", [None])[0]
            is_qual_raw = query.get("qualified", [None])[0]
            is_qual = is_qual_raw.lower() in ("true", "1") if is_qual_raw is not None else None
            candidates = self.service.get_continuous_evaluator_candidates(
                strategy=strategy,
                outcome=outcome,
                state=state,
                market=market,
                is_qualified=is_qual,
            )
            self._send_json(200, {"candidates": candidates, "count": len(candidates)})
            return

        elif path in ("/api/evaluator/qualified", "/api/qualified"):
            strategy = query.get("strategy", [None])[0]
            market = query.get("market", [None])[0]
            min_p = query.get("min_profit", [None])[0]
            min_profit_cents = int(min_p) if min_p and min_p.isdigit() else None
            qualified = self.service.get_continuous_evaluator_qualified_candidates(
                strategy=strategy,
                market=market,
                min_profit_cents=min_profit_cents,
            )
            self._send_json(200, {"qualified_candidates": qualified, "count": len(qualified)})
            return

        elif path in ("/api/evaluator/history",):
            limit_str = query.get("limit", ["100"])[0]
            try:
                limit = max(1, min(1000, int(limit_str)))
            except ValueError:
                limit = 100
            candidate_id = query.get("candidate_id", [None])[0]
            to_state = query.get("to_state", [None])[0]
            outcome = query.get("outcome", [None])[0]
            history = self.service.get_continuous_evaluator_history(
                limit=limit,
                candidate_id=candidate_id,
                to_state=to_state,
                outcome=outcome,
            )
            self._send_json(200, {"history": history, "count": len(history)})
            return

        elif path in ("/api/alerts", "/api/alerts/status"):
            alert_status = self.service.get_alert_service_status()
            self._send_json(200, alert_status)
            return

        elif path in ("/api/alerts/history",):
            limit_str = query.get("limit", ["50"])[0]
            try:
                limit = max(1, min(500, int(limit_str)))
            except ValueError:
                limit = 50
            event_type = query.get("event_type", [None])[0]
            candidate_id = query.get("candidate_id", [None])[0]
            alerts = self.service.get_alert_history(
                limit=limit,
                event_type=event_type,
                candidate_id=candidate_id,
            )
            self._send_json(200, {"alerts": alerts, "count": len(alerts)})
            return

        elif path in ("/api/scanner/sessions", "/api/breadth/sessions"):
            sessions = self.service.get_breadth_scanner_sessions()
            self._send_json(200, {"sessions": sessions, "count": len(sessions)})
            return

        elif path in ("/api/scanner/status", "/api/breadth/status"):
            scanner_dir = query.get("dir", [None])[0] or query.get("scanner_dir", [None])[0]
            scanner_status = self.service.get_breadth_scanner_status(scanner_dir=scanner_dir)
            self._send_json(200, scanner_status)
            return

        elif path in ("/api/scanner/strategies", "/api/breadth/strategies"):
            scanner_dir = query.get("dir", [None])[0] or query.get("scanner_dir", [None])[0]
            strat_summary = self.service.get_breadth_scanner_strategy_breakdown(scanner_dir=scanner_dir)
            self._send_json(200, strat_summary)
            return

        elif path in ("/api/scanner/summaries", "/api/breadth/summaries"):
            scanner_dir = query.get("dir", [None])[0] or query.get("scanner_dir", [None])[0]
            limit_str = query.get("limit", ["50"])[0]
            try:
                limit = max(1, min(500, int(limit_str)))
            except ValueError:
                limit = 50
            summaries = self.service.get_breadth_scanner_summaries(scanner_dir=scanner_dir, limit=limit)
            self._send_json(200, {"summaries": summaries, "count": len(summaries)})
            return

        elif path in ("/api/scanner/candidates", "/api/breadth/candidates"):
            scanner_dir = query.get("dir", [None])[0] or query.get("scanner_dir", [None])[0]
            limit_str = query.get("limit", ["100"])[0]
            try:
                limit = max(1, min(1000, int(limit_str)))
            except ValueError:
                limit = 100
            strategy = query.get("strategy", [None])[0]
            cycle_raw = query.get("cycle", [None])[0]
            cycle = int(cycle_raw) if cycle_raw and cycle_raw.isdigit() else None
            candidates = self.service.get_breadth_scanner_candidates(
                scanner_dir=scanner_dir,
                limit=limit,
                strategy=strategy,
                cycle_number=cycle,
            )
            self._send_json(200, {"candidates": candidates, "count": len(candidates)})
            return

        elif path in ("/api/scanner/qualifications", "/api/breadth/qualifications"):
            scanner_dir = query.get("dir", [None])[0] or query.get("scanner_dir", [None])[0]
            limit_str = query.get("limit", ["100"])[0]
            try:
                limit = max(1, min(1000, int(limit_str)))
            except ValueError:
                limit = 100
            strategy = query.get("strategy", [None])[0]
            outcome = query.get("outcome", [None])[0]
            is_qual_raw = query.get("qualified", [None])[0]
            is_qual = is_qual_raw.lower() in ("true", "1") if is_qual_raw is not None else None
            cycle_raw = query.get("cycle", [None])[0]
            cycle = int(cycle_raw) if cycle_raw and cycle_raw.isdigit() else None
            qualifications = self.service.get_breadth_scanner_qualifications(
                scanner_dir=scanner_dir,
                limit=limit,
                strategy=strategy,
                outcome=outcome,
                is_qualified=is_qual,
                cycle_number=cycle,
            )
            self._send_json(200, {"qualifications": qualifications, "count": len(qualifications)})
            return

        elif path == "/api/datasets":
            datasets = self.service.get_available_datasets()
            self._send_json(200, {"datasets": datasets, "count": len(datasets)})
            return

        # 404 Not Found
        self._send_json(404, {"error": "Not Found", "path": path})

    def _serve_static_file(self, filename: str, content_type: str) -> None:
        file_path = STATIC_DIR / filename
        if not file_path.exists():
            self._send_json(404, {"error": f"Static file {filename} not found"})
            return

        try:
            with open(file_path, "rb") as f:
                content = f.read()

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(content)
        except Exception as exc:
            logger.exception(f"Error serving {filename}: {exc}")
            self._send_json(500, {"error": f"Failed to read static asset: {exc}"})

    def _send_json(self, status_code: int, data: dict) -> None:
        try:
            body = json.dumps(data, indent=2).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            logger.exception(f"Error sending JSON response: {exc}")

    def log_message(self, format: str, *args: object) -> None:
        """Silence standard console logging to keep stdout clean, or log to debug."""
        logger.debug("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))


class DashboardServer:
    """Encapsulates the HTTP server with background execution and clean shutdown."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        workspace_root: Optional[Path] = None,
        scanner_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.workspace_root = workspace_root
        self.scanner_dir = scanner_dir

        # Configure handler service with workspace root and optional scanner directory
        DashboardRequestHandler.service = DashboardDataService(
            workspace_root=workspace_root,
            default_scanner_dir=scanner_dir,
        )

        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, blocking: bool = False) -> str:
        """Start server. If blocking is True, runs until interrupted."""
        # Handle port fallback if in use
        current_port = self.port
        while current_port < self.port + 50:
            try:
                self._server = HTTPServer((self.host, current_port), DashboardRequestHandler)
                self.port = self._server.server_port
                break
            except OSError as exc:
                if "Address already in use" in str(exc):
                    current_port += 1
                else:
                    raise

        if not self._server:
            raise RuntimeError(f"Could not bind to {self.host} on ports {self.port}..{current_port}")

        url = f"http://{self.host}:{self.port}"
        logger.info(f"Dashboard server started at {url}")

        if blocking:
            try:
                self._server.serve_forever()
            except KeyboardInterrupt:
                self.stop()
        else:
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()

        return url

    def stop(self) -> None:
        """Clean shutdown of server."""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("Dashboard server cleanly stopped.")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def service(self) -> DashboardDataService:
        return DashboardRequestHandler.service


def create_dashboard_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    workspace_root: Optional[Path] = None,
    scanner_dir: Optional[Union[str, Path]] = None,
) -> DashboardServer:
    """Factory function for DashboardServer."""
    return DashboardServer(
        host=host,
        port=port,
        workspace_root=workspace_root,
        scanner_dir=scanner_dir,
    )
