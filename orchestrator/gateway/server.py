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

TOKEN = os.environ.get("CODEFACTORY_AUTH_TOKEN", "")

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


class CredentialError(Exception):
    pass


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
    serialized = json.dumps(payload)
    if (any(token and token in serialized for token in (*tokens, TOKEN))
            or re.search(r"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{16,}", serialized)):
        raise ValueError("credential-in-payload")
    def inspect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if re.search(r"token|secret|password|authorization|credential", key, re.IGNORECASE):
                    raise ValueError("credential-in-payload")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
    inspect(payload)


def _validate_repository(repo, issue, token):
    if not repo or not token or len(token) > 4096 or any(char.isspace() for char in token) or token == TOKEN:
        raise CredentialError("repository-credential-required")
    try:
        metadata = _github("GET", "/repos/" + quote(repo, safe="/"), token=token)
        if metadata.get("full_name", "").lower() != repo.lower():
            raise CredentialError("repository-identity-mismatch")
        if metadata.get("permissions", {}).get("push") is not True or metadata.get("archived"):
            raise CredentialError("repository-write-access-unverified")
        for path in ("/contents", "/pulls?per_page=1", "/issues?per_page=1"):
            _github("GET", "/repos/" + quote(repo, safe="/") + path, token=token)
        if issue:
            remote = _github("GET", f"/repos/{quote(repo, safe='/')}/issues/{issue}", token=token)
            if remote.get("number") != issue or "pull_request" in remote or remote.get("html_url", "").lower() != f"https://github.com/{repo}/issues/{issue}".lower():
                raise CredentialError("repository-issue-mismatch")
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 404):
            raise CredentialError("repository-credential-denied") from None
        raise CredentialError("repository-validation-unavailable") from None
    except (OSError, ValueError, TypeError, AttributeError):
        raise CredentialError("repository-validation-unavailable") from None


def _create_task_secret(task_id, repo, token, callback):
    name = "task-credential-" + task_id
    expires_at = str(time.time() + int(os.environ.get("TASK_CREDENTIAL_TTL_SECONDS", "86400")))
    secret = {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque", "immutable": True,
        "metadata": {"name": name, "namespace": POD_NAMESPACE,
                     "labels": {"code-factory-task": task_id},
                     "annotations": {"repository": repo.lower(), "expires-at": expires_at}},
        "data": {key: base64.b64encode(value.encode()).decode() for key, value in
                 (("repository-token", token), ("callback-token", callback),
                  ("expires-at", expires_at))},
    }
    _k8s_request("POST", _secret_path(), secret)
    return name


def _task_repository_token(task, check_expiry=True):
    if not task["credential_ref"]:
        raise CredentialError("credential-missing")
    try:
        secret = _k8s_request("GET", _secret_path(task["credential_ref"]))
    except KubernetesError as error:
        if error.status == 404:
            raise CredentialError("credential-missing") from None
        raise
    _validate_secret_binding(task, secret)
    metadata = secret["metadata"]
    try:
        if check_expiry and float(metadata["annotations"]["expires-at"]) <= time.time():
            raise CredentialError("credential-expired")
        return base64.b64decode(secret["data"]["repository-token"], validate=True).decode()
    except (ValueError, KeyError, UnicodeError):
        raise CredentialError("credential-invalid") from None


def _validate_secret_binding(task, secret):
    metadata = secret.get("metadata", {})
    if metadata.get("labels", {}).get("code-factory-task") != task["task_id"] or metadata.get("annotations", {}).get("repository") != task["repo"].lower():
        raise CredentialError("credential-binding-mismatch")
    try:
        callback = base64.b64decode(secret["data"]["callback-token"], validate=True)
    except (ValueError, KeyError):
        raise CredentialError("credential-invalid") from None
    if not task["callback_hash"] or not hmac.compare_digest(hashlib.sha256(callback).hexdigest(), task["callback_hash"]):
        raise CredentialError("credential-binding-mismatch")


def _delete_task_secret(task):
    if task["credential_ref"]:
        try:
            secret = _k8s_request("GET", _secret_path(task["credential_ref"]))
            _validate_secret_binding(task, secret)
            uid = secret["metadata"].get("uid")
            options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}} if uid else None
            _k8s_request("DELETE", _secret_path(task["credential_ref"]), options)
        except KubernetesError as error:
            if error.status != 404:
                raise


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
        columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
        for name in ("credential_ref", "callback_hash"):
            if name not in columns:
                db.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT")
        if "credential_cleanup" not in columns:
            db.execute("ALTER TABLE tasks ADD COLUMN credential_cleanup TEXT NOT NULL DEFAULT 'retained'")
    return db


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
    if not task["issue"]:
        return
    repo, issue = task["repo"], task["issue"]
    if not _repo_allowed(repo):
        raise ValueError("repo-not-allowed")
    token = _task_repository_token(task)
    def github(method, path, body=None):
        return _github(method, path, body, token=token)
    base = f"/repos/{quote(repo, safe='/')}/issues/{issue}"
    remote = github("GET", base)
    if remote.get("number") != issue or remote.get("html_url", "").lower() != f"https://github.com/{repo}/issues/{issue}".lower() or "pull_request" in remote:
        raise ValueError("github-issue-mismatch")
    labels = github("GET", base + "/labels?per_page=100")
    names = {label["name"] for label in labels}
    for name in ("queued", "done" if task["status"] == "failed" else "failed"):
        if name in names:
            github("DELETE", base + "/labels/" + quote(name, safe=""))
    github("POST", base + "/labels", {"labels": ["done" if task["status"] == "completed" else "failed"]})
    result = json.loads(task["result"])
    marker = f"<!-- code-factory:{task['task_id']} -->"
    message = f"{marker}\nCode Factory: {task['status']}."
    if task["status"] == "completed" and result.get("pr_url"):
        message += "\n" + result["pr_url"]
    found = False
    for page in range(1, 11):
        comments = github("GET", base + f"/comments?per_page=100&page={page}")
        if any(marker in comment.get("body", "") for comment in comments):
            found = True
            break
        if len(comments) < 100:
            break
    else:
        raise ValueError("comment-history-too-large")
    if not found:
        github("POST", base + "/comments", {"body": message})


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
        fatal = {"credential-missing", "credential-expired", "credential-denied", "credential-binding-mismatch", "credential-invalid"}
        try:
            if task["lifecycle"] in fatal:
                raise CredentialError(task["lifecycle"])
            if task["lifecycle"] != "cleanup-pending":
                _sync_lifecycle(task)
            db.execute("UPDATE tasks SET lifecycle='cleanup-pending' WHERE task_id=?", (task_id,))
            db.execute("UPDATE tasks SET credential_cleanup='pending' WHERE task_id=?", (task_id,))
            _delete_task_secret(task)
            db.execute("UPDATE tasks SET credential_cleanup='deleted' WHERE task_id=?", (task_id,))
            status = "synced"
        except CredentialError as error:
            status = str(error)
        except urllib.error.HTTPError as error:
            status = "credential-denied" if error.code in (401, 403, 404) else "pending"
        except Exception:  # noqa: BLE001
            logger.warning("github-lifecycle-pending task=%s", task_id)
            status = "cleanup-pending" if db.execute("SELECT lifecycle FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0] == "cleanup-pending" else "pending"
        if status in fatal:
            db.execute("UPDATE tasks SET credential_cleanup='pending' WHERE task_id=?", (task_id,))
            try:
                _delete_task_secret(task)
                db.execute("UPDATE tasks SET credential_cleanup='deleted' WHERE task_id=?", (task_id,))
            except (KubernetesError, CredentialError):
                logger.warning("credential-cleanup-pending task=%s", task_id)
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
    if type(task_id) is int and task_id >= 0:
        task_id = str(task_id)
    if (
        not isinstance(task_id, str)
        or task_id == "unknown"
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?", task_id)
    ):
        raise ValueError("invalid-assigned-task-id")
    return task_id


def _task_job_ttl_seconds():
    try:
        ttl = int(os.environ.get("TASK_JOB_TTL_SECONDS_AFTER_FINISHED", "86400"))
    except ValueError:
        raise ValueError("TASK_JOB_TTL_SECONDS_AFTER_FINISHED must be an integer from 1 to 604800") from None
    if not 1 <= ttl <= 604800:
        raise ValueError("TASK_JOB_TTL_SECONDS_AFTER_FINISHED must be an integer from 1 to 604800")
    return ttl


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
            if entry["name"] not in ("ASSIGNED_TASK_ID", "ASSIGNED_TASK_PAYLOAD", "GITHUB_TOKEN", "REPO_GITHUB_PAT", "TASK_CALLBACK_TOKEN", "TASK_REPOSITORY", "TASK_CREDENTIAL_EXPIRES_AT")
        ] + [
            {"name": "ASSIGNED_TASK_ID", "value": assigned_id},
            {"name": "ASSIGNED_TASK_PAYLOAD", "value": json.dumps(payload)},
            {"name": "TASK_REPOSITORY", "value": _identity(payload)[0]},
                        *[{"name": name, "valueFrom": {"secretKeyRef": {"name": "task-credential-" + assigned_id, "key": key}}}
                            for name, key in (("REPO_GITHUB_PAT", "repository-token"), ("TASK_CALLBACK_TOKEN", "callback-token"), ("TASK_CREDENTIAL_EXPIRES_AT", "expires-at"))],
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
            "ttlSecondsAfterFinished": _task_job_ttl_seconds(),
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
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _authorized(self):
        return bool(TOKEN) and hmac.compare_digest(self.headers.get("Authorization", "").encode(), f"token {TOKEN}".encode())

    def _lookup(self, db):
        parsed = urlsplit(self.path)
        task_id = parsed.path.removeprefix("/api/v1/tasks/").removesuffix("/sync")
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
                                     "lifecycle": task["lifecycle"], "credential_cleanup": task["credential_cleanup"],
                                     "job": json.loads(task["job"]) if task["job"] else None})
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
            elif urlsplit(self.path).path.endswith("/sync") and self.path.startswith("/api/v1/tasks/"):
                self._resync(payload)
            elif self.path.startswith("/api/v1/tasks/"):
                self._result(payload)
            else:
                self._json(404, {"error": "not-found"})
        except PermissionError:
            self._json(403, {"error": "repo-not-allowed"})
        except CredentialError as error:
            self._json(403, {"error": str(error)})
        except KubernetesError:
            self._json(503, {"error": "credential-store-unavailable"})
        except (ValueError, TypeError, UnicodeDecodeError):
            self._json(400, {"error": "invalid-task-or-result"})

    def _spawn(self, payload):
        token = self.headers.get("X-Repository-Token", "")
        _reject_credentials(payload, token)
        repo, issue = _identity(payload)
        if "worker_id" in payload:
            raise ValueError("gateway-assignment-required")
        if _is_external_workflow_payload(payload) and issue is None:
            raise ValueError("issue-required")
        if "task_id" in payload:
            _validate_assigned_task_id(payload["task_id"])
            if issue is not None and str(payload["task_id"]) != str(issue):
                raise ValueError("issue-mismatch")
        if not _repo_allowed(repo):
            raise PermissionError("repo-not-allowed")
        if not self._transport_allowed():
            raise CredentialError("transport-not-allowed")
        _validate_repository(repo, issue, token)
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
            callback = secrets.token_urlsafe(32)
            credential_ref = "task-credential-" + task_id
            db.execute("INSERT INTO tasks(task_id,repo,issue,status,payload,credential_ref,callback_hash) VALUES(?,?,?,?,?,?,?)",
                       (task_id, repo, issue, "queued", json.dumps(assigned), credential_ref, hashlib.sha256(callback.encode()).hexdigest()))
            db.commit()
            try:
                _create_task_secret(task_id, repo, token, callback)
            except KubernetesError:
                failure = json.dumps({"task_id": task_id, "repo": repo, "issue_number": issue,
                                      "status": "failed", "error": "credential-create-failed"})
                db.execute("UPDATE tasks SET status='failed', result=? WHERE task_id=?", (failure, task_id))
                _retry_lifecycle(task_id)
                self._json(503, {"task_id": task_id, "error": "credential-create-failed"})
                return
            job = None
            if "worker_id" not in payload:
                try:
                    job = _create_worker_job(POD_NAMESPACE, WORKER_JOB_TEMPLATE, task_id, assigned)
                except (OSError, ValueError, KeyError, RuntimeError, StopIteration, KubernetesError):
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

    def _resync(self, payload):
        if payload:
            raise ValueError("sync-body-must-be-empty")
        db = _task_db()
        try:
            task = self._lookup(db)
            if task is None:
                self._json(404, {"error": "task-not-found"})
                return
            if task["status"] not in ("completed", "failed") or not task["result"]:
                self._json(409, {"error": "task-not-terminal"})
                return
        finally:
            db.close()
        lifecycle = _retry_lifecycle(task["task_id"])
        self._json(200, {"task_id": task["task_id"], "status": task["status"], "lifecycle": lifecycle})

    def _result(self, payload):
        _reject_credentials(payload, self.headers.get("X-Task-Token", ""))
        repo, issue = _identity(payload)
        db = _task_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = self._lookup(db)
            if task is None:
                db.commit()
                self._json(404, {"error": "task-not-found"})
                return
            callback = self.headers.get("X-Task-Token", "")
            if not callback or not task["callback_hash"] or not hmac.compare_digest(hashlib.sha256(callback.encode()).hexdigest(), task["callback_hash"]):
                raise PermissionError("task-callback-denied")
            if task["lifecycle"] != "synced" and (not task["result"] or json.loads(task["result"]) != payload):
                try:
                    _reject_credentials(payload, _task_repository_token(task, check_expiry=False))
                except (CredentialError, KubernetesError):
                    pass
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
        logger.info("%s request", self.command)

def _create_server():
    mode = os.environ.get("GATEWAY_TRANSPORT_MODE", "")
    if mode not in ("edge-tls", "local-http"):
        raise ValueError("gateway-transport-mode-required")
    address = "127.0.0.1" if mode == "local-http" else "0.0.0.0"
    return ThreadingHTTPServer((address, 8000), Handler)


if __name__ == "__main__":
    server = _create_server()
    logger.info("gateway starting in %s mode", os.environ["GATEWAY_TRANSPORT_MODE"])
    logger.info("ALLOWED_REPOS=%r", ALLOWED_REPOS)
    server.serve_forever()
