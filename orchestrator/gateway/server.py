import base64
import copy
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from github_auth import (
    AppUnavailable,
    AuthenticationError,
    AuthorizationError,
    GitHubAuth,
)
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from security import (
    CredentialError,
)
from security import (
    identity as _security_identity,
)
from security import (
    is_external_workflow_payload as _security_is_external_workflow_payload,
)
from security import (
    reject_credentials as _security_reject_credentials,
)
from security import (
    validate_assigned_task_id as _security_validate_assigned_task_id,
)
from security import (
    validate_repository as _security_validate_repository,
)
from task_api import TaskAPI
from task_store import connect_task_database, retry_lifecycle

__all__ = ["copy", "hashlib", "parse_qs", "re", "secrets", "uuid"]

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_AUTH_TOKEN", "")

logger = logging.getLogger("gateway")
# StreamHandler with flushLevel=DEBUG forces every record to flush immediately.
# logging.basicConfig(stream=sys.stdout) uses block buffering when stdout is a
# pipe (e.g. inside a Kubernetes pod), so INFO lines are lost until the buffer
# fills or the process exits — which is why the pod shows no output.
stream = logging.StreamHandler(sys.stdout)
stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
stream.flushLevel = logging.DEBUG
logger.addHandler(stream)
logger.setLevel(logging.INFO)
logger.propagate = False

POD_NAMESPACE = os.environ.get("POD_NAMESPACE", "orchestrator")
WORKER_JOB_TEMPLATE = os.environ.get("WORKER_JOB_TEMPLATE", "orchestrator-worker")

SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
DB_INIT_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _github_app():
    return GitHubAuth(os.environ)


class KubernetesError(Exception):
    def __init__(self, status=None):
        self.status = status
        super().__init__("kubernetes-request-failed")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect-rejected")


def _secret_path(name=""):
    return f"/api/v1/namespaces/{POD_NAMESPACE}/secrets" + ("/" + name if name else "")


def _reject_credentials(payload, *tokens):
    return _security_reject_credentials(payload, *tokens)


def _validate_repository(repo, issue, token):
    return _security_validate_repository(repo, issue, token, _github)


def _create_task_secret(task_id, repo, callback):
    name = "task-credential-" + task_id
    expires_at = str(time.time() + int(os.environ.get("TASK_CREDENTIAL_TTL_SECONDS", "86400")))
    secret = {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque", "immutable": True,
        "metadata": {"name": name, "namespace": POD_NAMESPACE,
                     "labels": {"code-factory-task": task_id},
                     "annotations": {"repository": repo.lower(), "expires-at": expires_at}},
        "data": {key: base64.b64encode(value.encode()).decode() for key, value in
                 (("repository-auth", "github-app"), ("callback-token", callback),
                  ("expires-at", expires_at))},
    }
    response = _k8s_request("POST", _secret_path(), secret)
    uid = response.get("metadata", {}).get("uid")
    if not isinstance(uid, str) or not uid:
        raise KubernetesError()
    return uid


def _delete_task_secret(task):
    if task["credential_ref"]:
        if not task["credential_uid"]:
            raise KubernetesError(409)
        try:
            _k8s_request("DELETE", _secret_path(task["credential_ref"]), {
                "apiVersion": "v1", "kind": "DeleteOptions",
                "preconditions": {"uid": task["credential_uid"]},
            })
        except KubernetesError as error:
            if error.status != 404:
                raise


def _identity(payload):
    return _security_identity(payload)


def _task_db():
    path = os.environ.get("TASK_DB_PATH", "/data/tasks/tasks.sqlite3")
    return connect_task_database(path, DB_INIT_LOCK)


def _github(method, path, body=None, *, token):
    request = urllib.request.Request(
        "https://api.github.com" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"},
    )
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("github-response-too-large")
        return json.loads(raw) if raw else None


def _sync_lifecycle(task):
    return None


def _retry_lifecycle(task_id):
    return retry_lifecycle(task_id, _task_db, _delete_task_secret, KubernetesError, logger)


def _is_external_workflow_payload(payload):
    return _security_is_external_workflow_payload(payload)


def _k8s_base_url():
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    if not host:
        raise RuntimeError("missing-k8s-service-host")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    return f"https://{host}:{port}"


def _k8s_bearer_token():
    with open(SA_TOKEN_PATH, "r", encoding="utf-8") as token_file:
        return token_file.read().strip()


def _k8s_ssl_context():
    return ssl.create_default_context(cafile=SA_CA_PATH)


def _k8s_request(method, path, body=None):
    token = _k8s_bearer_token()
    ctx = _k8s_ssl_context()
    url = f"{_k8s_base_url()}{path}"
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ctx)).open(req, timeout=10) as resp:
            raw = resp.read() or b"{}"
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as error:
        logger.error(
            "k8s-request-failed method=%s path=%s status=%s",
            method, path, error.code,
        )
        raise KubernetesError(error.code) from None
    except (OSError, ValueError):
        raise KubernetesError() from None


def _validate_assigned_task_id(task_id):
    return _security_validate_assigned_task_id(task_id)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
# Application-level observability for the task-acceptor gateway. Scraped by the
# central Prometheus server on port 9000 (kept-vs-defer: no second stack).
#   * http_requests_total{method, path, status}  — request counter
#   * http_request_duration_seconds              — request latency histogram
#   * http_request_errors_total                  — error counter
#   * gateway_active_requests_gauge              — in-flight gauge
#   * gateway_healthz_gauge                      — readiness flag
#   * gateway_scrape_interval_seconds            — last /metrics scrape age
# --------------------------------------------------------------------------- #
REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests received, by method, path and status code.",
    ["method", "path", "status"],
)
ACCEPTED_TASKS = Counter(
    "gateway_tasks_accepted_total",
    "Durable tasks accepted by the gateway, by intake kind.",
    ["kind"],
)
DURATION = Histogram(
    "http_request_duration_seconds",
    "Request duration in seconds.",
    ["path", "method"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
ERRORS = Counter(
    "http_request_errors_total",
    "Total HTTP requests that returned a non-2xx status.",
    ["path"],
)
ACTIVE = Gauge("gateway_active_requests_gauge", "Number of in-flight requests.")
HEALTHZ = Gauge("gateway_healthz_gauge", "Readiness flag; 1 when /healthz is OK.")
SCRAPE = Gauge(
    "gateway_scrape_interval_seconds",
    "Age of the last /metrics scrape in seconds.",
)
TASKS_BY_STATUS = Gauge(
    "gateway_tasks_by_status",
    "Number of durable tasks in the task database, by current status.",
    ["status"],
)


def _refresh_task_state_metrics():
    TASKS_BY_STATUS.clear()
    path = os.environ.get("TASK_DB_PATH", "/data/tasks/tasks.sqlite3")
    if not os.path.exists(path):
        return
    db = _task_db()
    try:
        counts = db.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()
    finally:
        db.close()
    for status, count in counts:
        TASKS_BY_STATUS.labels(status=status).set(count)

class Handler(BaseHTTPRequestHandler):
    def _transport_allowed(self):
        mode = os.environ.get("GATEWAY_TRANSPORT_MODE", "")
        if mode == "edge-tls":
            return True
        return mode == "local-http" and ipaddress.ip_address(self.client_address[0]).is_loopback

    def _metric_path(self):
        path = urlsplit(self.path).path
        if path in ("/", "/healthz", "/metrics", "/api/v1/tasks/spawn"):
            return path
        return "/api/v1/tasks/:id" if path.startswith("/api/v1/tasks/") else "other"

    def _json(self, status, body):
        if getattr(self, "command", "POST") == "POST":
            self._post_record(status)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _authorized(self):
        self.actions_repo = None
        if "X-Repository-Token" in self.headers or not self._transport_allowed():
            return False
        path = urlsplit(self.path).path
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer ") and (path == "/api/v1/tasks/spawn" or path.endswith("/retry")):
            self.actions_repo = _github_app().authenticate(authorization[7:])
            return True
        management_token = os.environ.get("MANAGEMENT_SERVICE_TOKEN", "")
        return bool(management_token) and hmac.compare_digest(
            authorization.encode(), ("token " + management_token).encode())

    def _supervisor_authorized(self):
        return bool(SUPERVISOR_TOKEN) and hmac.compare_digest(
            self.headers.get("Authorization", "").encode(), f"token {SUPERVISOR_TOKEN}".encode())

    def _lookup(self, db):
        return self._task_api().lookup(db)

    def _task_api(self):
        return TaskAPI(self, sys.modules[__name__])

    def _post_record(self, status):
        """Record request metrics for the terminal POST response."""
        # Capture request start time for latency measurement.
        self.request_time = time.time()
        REQUESTS.labels(method="POST", path=self._metric_path(), status=str(status)).inc()
        if status >= 300:
            ERRORS.labels(path=self._metric_path()).inc()
        DURATION.labels(path=self._metric_path(), method="do_POST").observe(time.time() - self.request_time)

    def do_GET(self):
        # Capture request start time for latency measurement.
        self.request_time = time.time()
        # Instrumentation: count request and observe latency.
        REQUESTS.labels(method="GET", path=self._metric_path(), status="200").inc()
        DURATION.labels(path=self._metric_path(), method="do_GET").observe(time.time() - self.request_time)
        logger.info("GET request")
        path = self.path

        if path == "/healthz":
            logger.info("healthz check OK")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        elif path == "/metrics":
            logger.info("metrics scrape")
            SCRAPE.set(0)
            _refresh_task_state_metrics()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain;version=0.0.4")
            self.end_headers()
            self.wfile.write(generate_latest())
            return

        if urlsplit(path).path == "/api/v1/tasks":
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            self._task_api().list_tasks()
            return

        if path.startswith("/api/v1/tasks/"):
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            self._task_api().get_task()
            return

        if path != "/":
            self._json(404, {"error": "not-found"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        path = urlsplit(self.path).path
        task_callback = (path.startswith("/api/v1/tasks/") and path != "/api/v1/tasks/spawn"
                 and not path.endswith(("/retry", "/sync")))
        supervisor_endpoint = path.startswith("/api/v1/supervisor/tasks/")
        try:
            if "X-Repository-Token" in self.headers:
                self._json(400, {"error": "repository-token-header-unsupported"})
                return
            if not task_callback and not supervisor_endpoint and not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1048576:
                raise ValueError("invalid-body-size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or not isinstance(payload.get("payload", {}), dict):
                raise TypeError("invalid-payload")
            if path.endswith("/credentials") and (task_callback or supervisor_endpoint):
                self._task_api().credentials(payload, supervisor=supervisor_endpoint)
            elif urlsplit(self.path).path == "/api/v1/tasks/spawn":
                self._spawn(payload)
            elif urlsplit(self.path).path.startswith("/api/v1/supervisor/tasks/"):
                self._supervisor_result(payload)
            elif urlsplit(self.path).path.endswith("/retry") and self.path.startswith("/api/v1/tasks/"):
                self._retry(payload)
            elif urlsplit(self.path).path.endswith("/sync") and self.path.startswith("/api/v1/tasks/"):
                self._resync(payload)
            elif self.path.startswith("/api/v1/tasks/"):
                self._result(payload)
            else:
                self._json(404, {"error": "not-found"})
        except AuthenticationError:
            self._json(401, {"error": "github-identity-invalid"})
        except AuthorizationError:
            self._json(403, {"error": "repository-not-authorized"})
        except AppUnavailable:
            self._json(503, {"error": "github-app-unavailable"})
        except PermissionError:
            self._json(403, {"error": "repository-mismatch"})
        except CredentialError as error:
            self._json(403, {"error": str(error)})
        except KubernetesError:
            self._json(503, {"error": "credential-store-unavailable"})
        except (ValueError, TypeError, UnicodeDecodeError):
            self._json(400, {"error": "invalid-task-or-result"})

    def _retry(self, payload):
        return self._task_api().retry(payload)

    def _spawn(self, payload, retry_of=None, retry_key=None):
        return self._task_api().spawn(payload, retry_of, retry_key)

    def _resync(self, payload):
        return self._task_api().resync(payload)

    def _result(self, payload):
        return self._task_api().result(payload)

    def _supervisor_result(self, payload):
        return self._task_api().supervisor_result(payload)

    def log_message(self, format, *args):
        logger.info("%s request", self.command)

def _create_server():
    _github_app()
    management = os.environ.get("MANAGEMENT_SERVICE_TOKEN", "")
    if management and (len(management) < 32 or management == SUPERVISOR_TOKEN
                       or any(character.isspace() for character in management)):
        raise ValueError("management-service-credential-invalid")
    mode = os.environ.get("GATEWAY_TRANSPORT_MODE", "")
    if mode not in ("edge-tls", "local-http"):
        raise ValueError("gateway-transport-mode-required")
    address = "127.0.0.1" if mode == "local-http" else "0.0.0.0"
    return ThreadingHTTPServer((address, 8000), Handler)


if __name__ == "__main__":
    server = _create_server()
    logger.info("gateway starting in %s mode", os.environ["GATEWAY_TRANSPORT_MODE"])
    server.serve_forever()
