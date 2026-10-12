"""HTTP Server and request handler for the Kalshi Arbitrage Dashboard.

Built strictly using standard library http.server, requiring zero third-party web frameworks.
Exposes read-only REST endpoints and serves static UI assets.
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import logging
from pathlib import Path
import threading
from typing import Optional, Tuple
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

        if path == "/api/status":
            health = self.service.get_system_health()
            self._send_json(200, health)
            return

        elif path == "/api/overview":
            overview = self.service.get_overview(dataset_id)
            self._send_json(200, overview)
            return

        elif path == "/api/markets":
            markets = self.service.get_markets_table(dataset_id)
            self._send_json(200, {"markets": markets, "count": len(markets)})
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
    ) -> None:
        self.host = host
        self.port = port
        self.workspace_root = workspace_root

        # Configure handler service with workspace root
        if workspace_root:
            DashboardRequestHandler.service = DashboardDataService(workspace_root=workspace_root)
        else:
            DashboardRequestHandler.service = DashboardDataService()

        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, blocking: bool = False) -> str:
        """Start server. If blocking is True, runs until interrupted."""
        # Handle port fallback if in use
        current_port = self.port
        while current_port < self.port + 50:
            try:
                self._server = HTTPServer((self.host, current_port), DashboardRequestHandler)
                self.port = current_port
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


def create_dashboard_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    workspace_root: Optional[Path] = None,
) -> DashboardServer:
    """Factory function for DashboardServer."""
    return DashboardServer(host=host, port=port, workspace_root=workspace_root)
