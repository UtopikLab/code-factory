"""
Unit tests for the gateway observability endpoints (server.py).

These exercise the Prometheus /metrics endpoint (port-scraping), the /healthz
readiness probe, the liveness probe (GET /), and an end-to-end HTTP lifecycle
against a real ThreadingHTTPServer so the HTTP layer (including generate_latest)
is covered. All gateway metrics are prefixed http_/gateway_ (kept-vs-defer:
no cfe_ second stack; the cfe_ prefix applies to supervisor/worker components).
"""

import json
import socket
import threading
import time
from http.server import HTTPServer
from urllib.request import urlopen

import pytest
import server

# Fixed test port (gateway binds :8000 in production; this test uses an isolated
# port so it never collides).
TEST_PORT = 8099

# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _get(url):
    with urlopen(f"http://127.0.0.1:{TEST_PORT}{url}", timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _run_server():
    """Start a real HTTPServer serving the gateway Handler. Returns the server."""
    httpd = HTTPServer(("0.0.0.0", TEST_PORT), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            with socket.create_connection(("0.0.0.0", TEST_PORT), timeout=1):
                return httpd
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


@pytest.fixture(scope="module")
def running_server():
    httpd = _run_server()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


# --------------------------------------------------------------------------- #
# /metrics endpoint (Prometheus port-scraping)
# --------------------------------------------------------------------------- #


def test_metrics_route_serves_prometheus_text(running_server):
    status, body = _get("/metrics")
    assert status == 200
    assert body.startswith("#")
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body


def test_metrics_reports_all_components(running_server):
    _, body = _get("/metrics")
    assert "http_requests_total" in body
    assert "gateway_healthz_gauge" in body
    assert "http_request_duration_seconds" in body


def test_metrics_reports_durable_task_counts_by_current_status(monkeypatch, tmp_path):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))
    db = server._task_db()
    db.executemany(
        "INSERT INTO tasks(task_id, status, payload) VALUES(?,?,?)",
        [("queued-task", "queued", "{}"), ("running-task", "running", "{}"),
         ("failed-task", "failed", "{}"), ("another-failed-task", "failed", "{}")],
    )
    db.close()

    server._refresh_task_state_metrics()
    body = server.generate_latest().decode("utf-8")

    assert 'gateway_tasks_by_status{status="queued"} 1.0' in body
    assert 'gateway_tasks_by_status{status="running"} 1.0' in body
    assert 'gateway_tasks_by_status{status="failed"} 2.0' in body


def test_metrics_healthz_gauge(running_server):
    _, body = _get("/metrics")
    assert "gateway_healthz_gauge" in body


def test_metrics_exposition_is_parseable(running_server):
    _, body = _get("/metrics")
    lines = body.splitlines()
    assert any(l.startswith("#") for l in lines)
    assert any(l.startswith(("http_", "gateway_")) for l in lines)


# --------------------------------------------------------------------------- #
# /healthz readiness probe
# --------------------------------------------------------------------------- #


def test_healthz_readiness(running_server):
    status, _ = _get("/healthz")
    assert status == 200
    resp = json.loads(urlopen(f"http://127.0.0.1:{TEST_PORT}/healthz", timeout=5).read().decode())
    assert resp["status"] == "ok"


# --------------------------------------------------------------------------- #
# Liveness probe (GET /)
# --------------------------------------------------------------------------- #


def test_liveness_probe(running_server):
    status, _body = _get("/")
    assert status == 200
    resp = json.loads(urlopen(f"http://127.0.0.1:{TEST_PORT}/", timeout=5).read().decode())
    assert resp["status"] == "ok"


# --------------------------------------------------------------------------- #
# End-to-end HTTP server lifecycle
# --------------------------------------------------------------------------- #


def test_end_to_end_metrics_over_http(running_server):
    status, body = _get("/metrics")
    assert status == 200
    assert "http_requests_total" in body
    assert "gateway_healthz_gauge" in body
