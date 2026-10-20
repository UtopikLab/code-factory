import json
import logging
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import Counter, Gauge, Histogram, generate_latest

TOKEN = os.environ.get("GITHUB_TOKEN", "")

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

def _parse_allowed_repos(raw):
    """Return None for 'any repo', else a set of allowed owner/name repositories.

    An empty or whitespace-only value means 'allow all' (None), matching the
    wildcard default so that leaving the variable unset also opens the gateway.
    """
    raw = (raw or "").strip()
    if raw == "" or raw == "*":
        return None
    return {r for r in (x.strip() for x in raw.split(",")) if r}

# ALLOWED_REPOS controls which repositories may push tasks to the orchestrator.
#   - "*"            -> accept tasks from ANY repository (no validation)
#   - "a/b,c/d"      -> whitelist of specific owner/name repositories
#   - "a/b"          -> whitelist of a single repository
# The legacy "ALLOWED_REPO" variable is accepted for backward compatibility.
ALLOWED_REPOS_RAW = os.environ.get(
    "ALLOWED_REPOS", os.environ.get("ALLOWED_REPO", "*")
)
ALLOWED_REPOS = _parse_allowed_repos(ALLOWED_REPOS_RAW)

POD_NAMESPACE = os.environ.get("POD_NAMESPACE", "orchestrator")
WORKER_JOB_TEMPLATE = os.environ.get("WORKER_JOB_TEMPLATE", "orchestrator-worker")

SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


def _is_external_workflow_payload(payload):
    """True when payload appears to originate from the external drain workflow."""
    if not isinstance(payload, dict):
        return False
    if "worker_id" in payload:
        return False
    nested = payload.get("payload")
    return (
        "name" in payload
        and "ref" in payload
        and isinstance(nested, dict)
        and "issue_number" in nested
    )


def _sanitize_dns_label(value):
    """Return a RFC1123-compatible DNS label fragment."""
    cleaned = re.sub(r"[^a-z0-9-]", "-", str(value).lower())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "unknown"


def _build_worker_job_name(task_id):
    """Build unique, <=63-char Job name prefixed with orchestrator-worker."""
    prefix = "orchestrator-worker"
    ts = str(int(time.time()))
    task_part = _sanitize_dns_label(task_id)
    max_task_part = max(63 - (len(prefix) + len(ts) + 2), 1)
    task_part = task_part[:max_task_part].rstrip("-") or "x"
    return f"{prefix}-{task_part}-{ts}"


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
    with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
        raw = resp.read() or b"{}"
        return json.loads(raw.decode("utf-8"))


def _clone_job_from_template(template_job, namespace, job_name, task_id):
    template_spec = template_job.get("spec", {})
    source_labels = template_job.get("metadata", {}).get("labels", {}) or {}
    labels = dict(source_labels)
    labels["task-id"] = str(task_id)
    labels["triggered-by"] = "gateway"

    pod_template = template_spec.get("template", {}) or {}
    pod_meta = dict(pod_template.get("metadata") or {})
    pod_labels = dict(pod_meta.get("labels") or {})
    pod_labels.update(labels)
    pod_meta["labels"] = pod_labels
    pod_template = dict(pod_template)
    pod_template["metadata"] = pod_meta

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "parallelism": template_spec.get("parallelism"),
            "completions": template_spec.get("completions"),
            "backoffLimit": template_spec.get("backoffLimit"),
            "template": pod_template,
        },
    }


def _create_worker_job(namespace, template_name, task_id):
    template_path = f"/apis/batch/v1/namespaces/{namespace}/jobs/{template_name}"
    template_job = _k8s_request("GET", template_path)
    job_name = _build_worker_job_name(task_id)
    new_job = _clone_job_from_template(template_job, namespace, job_name, task_id)
    create_path = f"/apis/batch/v1/namespaces/{namespace}/jobs"
    created = _k8s_request("POST", create_path, body=new_job)
    return {
        "name": created.get("metadata", {}).get("name", job_name),
        "uid": created.get("metadata", {}).get("uid"),
    }

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

class Handler(BaseHTTPRequestHandler):
    def _post_record(self, status):
        """Record request metrics for the terminal POST response."""
        # Capture request start time for latency measurement.
        self.request_time = time.time()
        REQUESTS.labels(method="POST", path=self.path, status=str(status)).inc()
        if status >= 300:
            ERRORS.labels(path=self.path).inc()
        DURATION.labels(path=self.path, method="do_POST").observe(time.time() - self.request_time)

    def do_GET(self):
        # Capture request start time for latency measurement.
        self.request_time = time.time()
        # Instrumentation: count request and observe latency.
        REQUESTS.labels(method="GET", path=self.path, status="200").inc()
        DURATION.labels(path=self.path, method="do_GET").observe(time.time() - self.request_time)
        logger.info("GET %s", self.path)
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
            self.send_response(200)
            self.send_header("Content-Type", "text/plain;version=0.0.4")
            self.end_headers()
            self.wfile.write(generate_latest())
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        # Capture request start time for latency measurement.
        self.request_time = time.time()
        auth = self.headers.get("Authorization", "")
        if auth != f"token {TOKEN}":
            logger.warning("unauthorized request, auth=%r", auth)
            self._post_record(401)
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            logger.error("invalid-json: %s", body)
            self._post_record(400)
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"invalid-json"}')
            return

        repo = payload.get("repo") or payload.get("payload", {}).get("repo")
        if repo and ALLOWED_REPOS is not None and repo not in ALLOWED_REPOS:
            logger.warning("repo-not-allowed: repo=%r", repo)
            self._post_record(403)
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error":"repo-not-allowed"}')
            return

        response = {
            "status": "accepted",
            "task_id": payload.get("task_id") or payload.get("payload", {}).get("issue_number") or "unknown",
            "repo": repo or ALLOWED_REPOS_RAW,
            "endpoint": "/api/v1/tasks/spawn",
        }

        if _is_external_workflow_payload(payload):
            try:
                job_info = _create_worker_job(POD_NAMESPACE, WORKER_JOB_TEMPLATE, response["task_id"])
            except (OSError, ValueError, KeyError, RuntimeError, urllib.error.URLError):
                logger.exception("job-create-failed task_id=%r", response["task_id"])
                self._post_record(500)
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"job-create-failed"}')
                return
            response["job_name"] = job_info.get("name")
            if job_info.get("uid"):
                response["job_uid"] = job_info.get("uid")

        logger.info("task-accepted task_id=%r repo=%r", response["task_id"], response["repo"])
        self._post_record(202)
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def log_message(self, format, *args):
        logger.info("%s %s", self.command, self.path)

if __name__ == "__main__":
    logger.info("gateway starting on 0.0.0.0:8000")
    logger.info("ALLOWED_REPOS=%r", ALLOWED_REPOS)
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    server.serve_forever()
