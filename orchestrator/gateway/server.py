import copy
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

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
DB_INIT_LOCK = threading.Lock()


def _identity(payload):
    nested = payload.get("payload", {})
    repo = payload.get("repo") or nested.get("repo")
    if any(value is not None and not isinstance(value, str) for value in (payload.get("repo"), nested.get("repo"))):
        raise ValueError("invalid-repository")
    if payload.get("repo") and nested.get("repo") and payload["repo"].lower() != nested["repo"].lower():
        raise ValueError("repository-mismatch")
    issue = nested.get("issue_number", payload.get("issue_number"))
    if repo is not None and (not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
                             or any(part in (".", "..") for part in repo.split("/"))):
        raise ValueError("invalid-repository")
    if ("issue_number" in payload or "issue_number" in nested) and (type(issue) is not int or issue <= 0 or not repo):
        raise ValueError("invalid-issue")
    if "issue_number" in nested and "issue_number" in payload and nested["issue_number"] != payload["issue_number"]:
        raise ValueError("issue-mismatch")
    return repo, issue


def _repo_allowed(repo):
    return not repo or ALLOWED_REPOS is None or repo.lower() in {entry.lower() for entry in ALLOWED_REPOS}


def _task_db():
    path = os.environ.get("TASK_DB_PATH", "/data/tasks/tasks.sqlite3")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with DB_INIT_LOCK:
        db = sqlite3.connect(path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY, repo TEXT, issue INTEGER, status TEXT NOT NULL,
            payload TEXT NOT NULL, result TEXT, job TEXT, lifecycle TEXT NOT NULL DEFAULT 'pending',
            sync_started REAL NOT NULL DEFAULT 0)""")
    return db


def _github(method, path, body=None):
    request = urllib.request.Request(
        "https://api.github.com" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"},
    )
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError("github-redirect-rejected")
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("github-response-too-large")
        return json.loads(raw) if raw else None


def _sync_lifecycle(task):
    if not task["issue"]:
        return
    repo, issue = task["repo"], task["issue"]
    if not _repo_allowed(repo):
        raise ValueError("repo-not-allowed")
    base = f"/repos/{quote(repo, safe='/')}/issues/{issue}"
    remote = _github("GET", base)
    if remote.get("number") != issue or remote.get("html_url", "").lower() != f"https://github.com/{repo}/issues/{issue}".lower() or "pull_request" in remote:
        raise ValueError("github-issue-mismatch")
    labels = _github("GET", base + "/labels?per_page=100")
    names = {label["name"] for label in labels}
    for name in ("queued", "done" if task["status"] == "failed" else "failed"):
        if name in names:
            _github("DELETE", base + "/labels/" + quote(name, safe=""))
    _github("POST", base + "/labels", {"labels": ["done" if task["status"] == "completed" else "failed"]})
    result = json.loads(task["result"])
    marker = f"<!-- code-factory:{task['task_id']} -->"
    message = f"{marker}\nCode Factory: {task['status']}."
    if task["status"] == "completed" and result.get("pr_url"):
        message += "\n" + result["pr_url"]
    found = False
    for page in range(1, 11):
        comments = _github("GET", base + f"/comments?per_page=100&page={page}")
        if any(marker in comment.get("body", "") for comment in comments):
            found = True
            break
        if len(comments) < 100:
            break
    else:
        raise ValueError("comment-history-too-large")
    if not found:
        _github("POST", base + "/comments", {"body": message})


def _retry_lifecycle(task_id):
    db = _task_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task["lifecycle"] == "synced" or (task["lifecycle"] == "syncing" and time.time() - task["sync_started"] < 1800):
            db.commit()
            return task["lifecycle"]
        db.execute("UPDATE tasks SET lifecycle='syncing', sync_started=? WHERE task_id=?", (time.time(), task_id))
        db.commit()
        try:
            _sync_lifecycle(task)
            status = "synced"
        except Exception:  # noqa: BLE001
            logger.warning("github-lifecycle-pending task=%s", task_id)
            status = "pending"
        db.execute("UPDATE tasks SET lifecycle=? WHERE task_id=?", (status, task_id))
        return status
    finally:
        db.close()


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
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            raw = resp.read() or b"{}"
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as error:
        logger.error(
            "k8s-request-failed method=%s path=%s status=%s",
            method, path, error.code,
        )
        raise


def _validate_assigned_task_id(task_id):
    if type(task_id) is int and task_id >= 0:
        task_id = str(task_id)
    if (
        not isinstance(task_id, str)
        or task_id == "unknown"
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?", task_id)
    ):
        raise ValueError("invalid-assigned-task-id")
    return task_id


def _clone_job_from_template(template_job, namespace, job_name, task_id, payload=None):
    template_spec = template_job.get("spec", {})
    source_labels = template_job.get("metadata", {}).get("labels", {}) or {}
    labels = dict(source_labels)
    labels["task-id"] = str(task_id)
    labels["triggered-by"] = "gateway"

    pod_template = template_spec.get("template", {}) or {}
    pod_meta = dict(pod_template.get("metadata") or {})
    pod_labels = dict(pod_meta.get("labels") or {})
    pod_labels.update(labels)
    for key in (
        "controller-uid",
        "batch.kubernetes.io/controller-uid",
        "job-name",
        "batch.kubernetes.io/job-name",
    ):
        labels.pop(key, None)
        pod_labels.pop(key, None)
    pod_meta["labels"] = pod_labels
    pod_template = dict(pod_template)
    pod_template["metadata"] = pod_meta

    if payload is not None:
        assigned_id = _validate_assigned_task_id(task_id)
        pod_spec = copy.deepcopy(pod_template.get("spec", {}))
        coder = next(container for container in pod_spec.get("containers", []) if container["name"] == "coder")
        coder["env"] = [
            entry for entry in coder.get("env", [])
            if entry["name"] not in ("ASSIGNED_TASK_ID", "ASSIGNED_TASK_PAYLOAD")
        ] + [
            {"name": "ASSIGNED_TASK_ID", "value": assigned_id},
            {"name": "ASSIGNED_TASK_PAYLOAD", "value": json.dumps(payload)},
        ]
        pod_template["spec"] = pod_spec

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "parallelism": 1 if payload is not None else template_spec.get("parallelism"),
            "completions": 1 if payload is not None else template_spec.get("completions"),
            "backoffLimit": 0 if payload is not None else template_spec.get("backoffLimit"),
            "template": pod_template,
        },
    }


def _create_worker_job(namespace, template_name, task_id, payload):
    template_path = f"/apis/batch/v1/namespaces/{namespace}/jobs/{template_name}"
    template_job = _k8s_request("GET", template_path)
    job_name = _build_worker_job_name(task_id)
    new_job = _clone_job_from_template(template_job, namespace, job_name, task_id, payload)
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
    def _json(self, status, body):
        if getattr(self, "command", "POST") == "POST":
            self._post_record(status)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _authorized(self):
        return bool(TOKEN) and hmac.compare_digest(self.headers.get("Authorization", ""), f"token {TOKEN}")

    def _lookup(self, db):
        parsed = urlsplit(self.path)
        task_id = parsed.path.removeprefix("/api/v1/tasks/")
        query = parse_qs(parsed.query)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", task_id):
            raise ValueError("invalid-task-id")
        repo = query.get("repo", [None])[0]
        task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None and task_id.isdigit() and repo:
            task = db.execute("SELECT * FROM tasks WHERE lower(repo)=lower(?) AND issue=?", (repo, int(task_id))).fetchone()
        if task and (not _repo_allowed(task["repo"]) or (repo and repo.lower() != (task["repo"] or "").lower())):
            raise PermissionError("repo-not-allowed")
        return task

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

        if path.startswith("/api/v1/tasks/"):
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            db = _task_db()
            try:
                task = self._lookup(db)
                if task is None:
                    self._json(404, {"error": "task-not-found"})
                else:
                    self._json(200, {"task_id": task["task_id"], "repo": task["repo"], "issue_number": task["issue"],
                                     "status": task["status"], "result": json.loads(task["result"]) if task["result"] else None,
                                     "lifecycle": task["lifecycle"], "job": json.loads(task["job"]) if task["job"] else None})
            except PermissionError:
                self._json(403, {"error": "repo-not-allowed"})
            except ValueError:
                self._json(400, {"error": "invalid-task-id"})
            finally:
                db.close()
            return

        if path != "/":
            self._json(404, {"error": "not-found"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1048576:
                raise ValueError("invalid-body-size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or not isinstance(payload.get("payload", {}), dict):
                raise TypeError("invalid-payload")
            if urlsplit(self.path).path == "/api/v1/tasks/spawn":
                self._spawn(payload)
            elif self.path.startswith("/api/v1/tasks/"):
                self._result(payload)
            else:
                self._json(404, {"error": "not-found"})
        except PermissionError:
            self._json(403, {"error": "repo-not-allowed"})
        except (ValueError, TypeError, UnicodeDecodeError):
            self._json(400, {"error": "invalid-task-or-result"})

    def _spawn(self, payload):
        repo, issue = _identity(payload)
        if _is_external_workflow_payload(payload) and issue is None:
            raise ValueError("issue-required")
        if "task_id" in payload:
            _validate_assigned_task_id(payload["task_id"])
            if issue is not None and str(payload["task_id"]) != str(issue):
                raise ValueError("issue-mismatch")
        if not _repo_allowed(repo):
            raise PermissionError("repo-not-allowed")
        task_id = "issue-" + hashlib.sha256(f"{repo.lower()}#{issue}".encode()).hexdigest()[:32] if issue else uuid.uuid4().hex
        assigned = copy.deepcopy(payload)
        assigned["task_id"] = task_id
        if repo:
            assigned["repo"] = repo
        db = _task_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if old:
                db.commit()
                code = 409 if old["status"] == "failed" else 503 if old["status"] == "queued" else 202
                self._json(code, {"task_id": task_id, "repo": repo, "status": old["status"], "replayed": True})
                return
            db.execute("INSERT INTO tasks(task_id,repo,issue,status,payload) VALUES(?,?,?,?,?)",
                       (task_id, repo, issue, "queued", json.dumps(assigned)))
            db.commit()
            job = None
            if "worker_id" not in payload:
                try:
                    job = _create_worker_job(POD_NAMESPACE, WORKER_JOB_TEMPLATE, task_id, assigned)
                except (OSError, ValueError, KeyError, RuntimeError, StopIteration):
                    failure = json.dumps({"task_id": task_id, "repo": repo, "issue_number": issue,
                                          "status": "failed", "error": "job-create-failed"})
                    db.execute("UPDATE tasks SET status='failed', result=? WHERE task_id=? AND status='queued'", (failure, task_id))
                    _retry_lifecycle(task_id)
                    self._json(500, {"task_id": task_id, "error": "job-create-failed"})
                    return
            db.execute("UPDATE tasks SET status='running', job=? WHERE task_id=? AND status='queued'", (json.dumps(job), task_id))
            self._json(202, {"task_id": task_id, "repo": repo, "status": "running", "job_name": (job or {}).get("name")})
        finally:
            db.close()

    def _result(self, payload):
        repo, issue = _identity(payload)
        db = _task_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = self._lookup(db)
            if task is None:
                db.commit()
                self._json(404, {"error": "task-not-found"})
                return
            legacy_id = urlsplit(self.path).path.rsplit("/", 1)[1].isdigit() and repo is not None
            if str(payload.get("task_id")) != (str(task["issue"]) if legacy_id else task["task_id"]):
                raise ValueError("task-id-mismatch")
            if (repo or "").lower() != (task["repo"] or "").lower() or issue != task["issue"]:
                raise ValueError("result-identity-mismatch")
            if payload.get("status") not in ("completed", "failed"):
                raise ValueError("terminal-status-required")
            if payload["status"] == "completed" and (payload.get("error") or not payload.get("patch")):
                raise ValueError("empty-completion")
            original = json.loads(task["payload"])
            nested = original.get("payload", original)
            if (task["issue"] and not any(nested.get(key) or original.get(key) for key in ("files", "edits", "changes"))
                    and payload["status"] == "completed" and not re.fullmatch(
                        rf"https://github\.com/{re.escape(task['repo'])}/pull/[1-9][0-9]*", payload.get("pr_url", ""), re.IGNORECASE)):
                raise ValueError("completion-pr-required")
            serialized = json.dumps(payload, sort_keys=True)
            if task["result"] and json.loads(task["result"]) != payload:
                db.commit()
                self._json(409, {"error": "terminal-result-conflict"})
                return
            db.execute("UPDATE tasks SET status=?, result=? WHERE task_id=?", (payload["status"], serialized, task["task_id"]))
            db.commit()
            lifecycle = _retry_lifecycle(task["task_id"])
            self._json(200, {"task_id": task["task_id"], "status": payload["status"], "lifecycle": lifecycle})
        finally:
            db.close()

    def log_message(self, format, *args):
        logger.info("%s %s", self.command, self.path)

if __name__ == "__main__":
    logger.info("gateway starting on 0.0.0.0:8000")
    logger.info("ALLOWED_REPOS=%r", ALLOWED_REPOS)
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    server.serve_forever()
