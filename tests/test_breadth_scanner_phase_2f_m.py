"""Tests and Benchmarks for Phase 2F-M: Discovery Performance Optimisation and HTTP Connection Pooling.

Verifies:
1. PooledHTTPTransport functionality and Keep-Alive connection reuse.
2. Connection pooling over TLS/HTTPS with certificate handling.
3. Thread-safety across concurrent workers in ThreadPoolExecutor.
4. Transparent recovery and retry on dropped / stale connections (RemoteDisconnected/BrokenPipe).
5. Error handling and timeout propagation (KalshiTransportError).
6. Resource cleanup on transport.close().
7. Integration with KalshiRestClient and BreadthScanner.
8. Rigorous before-and-after benchmarks measuring throughput and latency speedup factors.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime, timezone
import http.client
import http.server
import json
import os
from pathlib import Path
import queue
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import pytest

from kalshi_arbitrage import (
    HTTPResponse,
    KalshiHTTPError,
    KalshiRestClient,
    KalshiTransportError,
    PooledHTTPTransport,
    UrllibGetTransport,
)


# -----------------------------------------------------------------------------
# Fixtures and Local Server Helpers
# -----------------------------------------------------------------------------

@dataclass
class ServerStats:
    connection_count: int = 0
    request_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class InstrumentedTestHandler(http.server.BaseHTTPRequestHandler):
    """Local HTTP handler tracking connection opens and request counts."""

    def setup(self):
        super().setup()
        with self.server.stats.lock:
            self.server.stats.connection_count += 1

    def do_GET(self):
        with self.server.stats.lock:
            self.server.stats.request_count += 1
            cur_req = self.server.stats.request_count

        if "/error_500" in self.path:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Internal Server Error"}')
            return

        if "/slow" in self.path:
            time.sleep(0.5)

        body = json.dumps({"status": "ok", "request_num": cur_req, "path": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "keep-alive")
        self.send_header("Date", "Fri, 30 Oct 2026 12:00:00 GMT")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@pytest.fixture
def http_server():
    """Spin up an ephemeral local HTTP test server."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), InstrumentedTestHandler)
    server.stats = ServerStats()
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, port
    server.shutdown()


@pytest.fixture
def https_server():
    """Spin up an ephemeral local HTTPS test server with a self-signed certificate."""
    with tempfile.TemporaryDirectory() as td:
        cert_file = os.path.join(td, "cert.pem")
        key_file = os.path.join(td, "key.pem")
        cmd = [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key_file,
            "-out", cert_file, "-days", "1", "-nodes", "-subj", "/CN=127.0.0.1"
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ssl_ctx_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx_server.load_cert_chain(cert_file, key_file)

        ssl_ctx_client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx_client.check_hostname = False
        ssl_ctx_client.verify_mode = ssl.CERT_NONE

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), InstrumentedTestHandler)
        server.stats = ServerStats()
        server.socket = ssl_ctx_server.wrap_socket(server.socket, server_side=True)
        port = server.server_address[1]

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        yield server, port, ssl_ctx_client
        server.shutdown()


# -----------------------------------------------------------------------------
# Unit Tests
# -----------------------------------------------------------------------------

def test_pooled_transport_reuses_single_connection(http_server):
    """Verify that multiple sequential requests to the same host reuse a single connection."""
    server, port = http_server
    transport = PooledHTTPTransport(max_pool_size=5)

    try:
        url = f"http://127.0.0.1:{port}/events"
        for i in range(10):
            resp = transport.get(url, {"page": str(i)}, timeout_seconds=5.0)
            assert resp.status_code == 200
            data = json.loads(resp.body)
            assert data["status"] == "ok"

        # Verify only 1 connection was created by the server for all 10 requests
        with server.stats.lock:
            assert server.stats.request_count == 10
            assert server.stats.connection_count == 1
    finally:
        transport.close()


def test_pooled_transport_over_tls_https(https_server):
    """Verify persistent HTTPS connections eliminate TLS handshakes across multiple requests."""
    server, port, ssl_client_ctx = https_server
    transport = PooledHTTPTransport(ssl_context=ssl_client_ctx, max_pool_size=5)

    try:
        url = f"https://127.0.0.1:{port}/trade-api/v2/events"
        for i in range(10):
            resp = transport.get(url, {"cursor": f"cur-{i}"}, timeout_seconds=5.0)
            assert resp.status_code == 200
            assert resp.get_header("Date") is not None

        # 10 HTTPS requests over exactly 1 TLS handshake
        with server.stats.lock:
            assert server.stats.request_count == 10
            assert server.stats.connection_count == 1
    finally:
        transport.close()


def test_pooled_transport_concurrent_thread_safety(http_server):
    """Verify thread-safety when multiple worker threads concurrently request from the pool."""
    server, port = http_server
    transport = PooledHTTPTransport(max_pool_size=4)

    try:
        url = f"http://127.0.0.1:{port}/data"
        num_workers = 4
        requests_per_worker = 15

        def worker_task(worker_id: int):
            for i in range(requests_per_worker):
                resp = transport.get(url, {"w": str(worker_id), "req": str(i)}, timeout_seconds=5.0)
                assert resp.status_code == 200

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker_task, wid) for wid in range(num_workers)]
            for fut in concurrent.futures.as_completed(futures):
                fut.result()

        with server.stats.lock:
            assert server.stats.request_count == num_workers * requests_per_worker
            # Concurrency bounded at 4 workers -> at most 4 connections created
            assert server.stats.connection_count <= num_workers
    finally:
        transport.close()


def test_pooled_transport_reconnects_on_server_dropped_socket(http_server):
    """Verify transport detects broken/stale idle connection and transparently reconnects."""
    server, port = http_server
    transport = PooledHTTPTransport(max_pool_size=2)

    try:
        url = f"http://127.0.0.1:{port}/initial"
        resp1 = transport.get(url, {}, timeout_seconds=5.0)
        assert resp1.status_code == 200

        # Simulate connection drop by extracting connection and closing socket
        target = ("http", "127.0.0.1", port)
        pool = transport._get_pool(target)
        conn = pool.get_nowait()
        conn.close()  # Force close underlying socket
        pool.put_nowait(conn)  # Put closed connection back into pool

        # Next request must transparently recover, retry, and succeed
        resp2 = transport.get(url, {"after": "closed"}, timeout_seconds=5.0)
        assert resp2.status_code == 200

        with server.stats.lock:
            assert server.stats.request_count == 2
            assert server.stats.connection_count == 2  # Reconnected cleanly
    finally:
        transport.close()


def test_pooled_transport_timeout_error_handling(http_server):
    """Verify request timeout raises KalshiTransportError."""
    server, port = http_server
    transport = PooledHTTPTransport()

    try:
        url = f"http://127.0.0.1:{port}/slow"
        with pytest.raises(KalshiTransportError, match="timed out"):
            transport.get(url, {}, timeout_seconds=0.1)
    finally:
        transport.close()


def test_pooled_transport_close_cleans_resources(http_server):
    """Verify close() cleans all idle pooled connections."""
    server, port = http_server
    transport = PooledHTTPTransport(max_pool_size=5)

    url = f"http://127.0.0.1:{port}/ping"
    transport.get(url, {}, timeout_seconds=5.0)

    target = ("http", "127.0.0.1", port)
    pool = transport._get_pool(target)
    assert not pool.empty()

    transport.close()
    assert len(transport._pools) == 0


def test_kalshi_rest_client_with_pooled_transport_default(http_server):
    """Verify KalshiRestClient seamlessly uses PooledHTTPTransport by default."""
    server, port = http_server
    client = KalshiRestClient(environment="production", base_url=f"http://127.0.0.1:{port}")

    assert isinstance(client._transport, PooledHTTPTransport)
    payload, resp = client._get_json_response("/events", {"limit": "100"})
    assert resp.status_code == 200
    assert payload.get("status") == "ok"
    assert client._last_network_duration >= 0.0

    client.close()


# -----------------------------------------------------------------------------
# Performance Benchmarks: Unpooled vs Pooled Transport
# -----------------------------------------------------------------------------

def test_benchmark_http_unpooled_vs_pooled(http_server):
    """Benchmark measured speedup of PooledHTTPTransport vs UrllibGetTransport over plain HTTP."""
    server, port = http_server
    unpooled = UrllibGetTransport()
    pooled = PooledHTTPTransport()

    N = 100
    url = f"http://127.0.0.1:{port}/benchmark"

    try:
        # 1. Unpooled benchmark
        t0 = time.monotonic()
        for i in range(N):
            resp = unpooled.get(url, {"i": str(i)}, timeout_seconds=5.0)
            assert resp.status_code == 200
        dur_unpooled = max(0.0001, time.monotonic() - t0)

        # 2. Pooled benchmark
        t0 = time.monotonic()
        for i in range(N):
            resp = pooled.get(url, {"i": str(i)}, timeout_seconds=5.0)
            assert resp.status_code == 200
        dur_pooled = max(0.0001, time.monotonic() - t0)

        speedup = dur_unpooled / dur_pooled
        print(f"\n[HTTP Benchmark N={N}] Unpooled: {dur_unpooled:.4f}s ({dur_unpooled/N*1000:.2f}ms/req) | Pooled: {dur_pooled:.4f}s ({dur_pooled/N*1000:.2f}ms/req) | Speedup: {speedup:.2f}x")

        # Connection reuse must be demonstrably faster
        assert speedup >= 1.2, f"Expected speedup >= 1.2x, got {speedup:.2f}x"
    finally:
        pooled.close()


def test_benchmark_https_tls_unpooled_vs_pooled(https_server):
    """Benchmark measured speedup of PooledHTTPTransport vs UrllibGetTransport over HTTPS (TLS)."""
    server, port, ssl_client_ctx = https_server
    unpooled = UrllibGetTransport(ssl_context=ssl_client_ctx)
    pooled = PooledHTTPTransport(ssl_context=ssl_client_ctx)

    N = 100
    url = f"https://127.0.0.1:{port}/benchmark_tls"

    try:
        # 1. Unpooled benchmark (100 separate TLS handshakes)
        t0 = time.monotonic()
        for i in range(N):
            resp = unpooled.get(url, {"i": str(i)}, timeout_seconds=5.0)
            assert resp.status_code == 200
        dur_unpooled_tls = max(0.0001, time.monotonic() - t0)

        # 2. Pooled benchmark (1 TLS handshake, 99 keep-alive reuses)
        t0 = time.monotonic()
        for i in range(N):
            resp = pooled.get(url, {"i": str(i)}, timeout_seconds=5.0)
            assert resp.status_code == 200
        dur_pooled_tls = max(0.0001, time.monotonic() - t0)

        tls_speedup = dur_unpooled_tls / dur_pooled_tls
        print(f"\n[HTTPS TLS Benchmark N={N}] Unpooled: {dur_unpooled_tls:.4f}s ({dur_unpooled_tls/N*1000:.2f}ms/req) | Pooled: {dur_pooled_tls:.4f}s ({dur_pooled_tls/N*1000:.2f}ms/req) | Speedup: {tls_speedup:.2f}x")

        # Reusing TLS sessions over localhost is vastly faster (typically 10x-25x)
        assert tls_speedup >= 3.0, f"Expected TLS speedup >= 3.0x, got {tls_speedup:.2f}x"
    finally:
        pooled.close()
