"""Durable task claiming and pinned worker-job construction."""

import copy
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse

import yaml
from jsonschema import Draft202012Validator

MAX_TOTAL_ATTEMPTS = 8
TASK_DB_PATH = os.environ.get("TASK_DB_PATH", "/data/tasks/tasks.sqlite3")


def task_database(path: str | None = None):
    path = path or TASK_DB_PATH
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("""CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY, repo TEXT, issue INTEGER, status TEXT NOT NULL,
        payload TEXT NOT NULL, result TEXT, job TEXT, lifecycle TEXT NOT NULL DEFAULT 'pending',
        sync_started REAL NOT NULL DEFAULT 0, credential_ref TEXT, callback_hash TEXT,
        retry_of TEXT, retry_key TEXT, credential_cleanup TEXT NOT NULL DEFAULT 'retained',
        attempts INTEGER NOT NULL DEFAULT 0, claim_owner TEXT, claimed_at REAL,
        config_version TEXT, config_hash TEXT, plan TEXT)""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
    for name, declaration in (("attempts", "INTEGER NOT NULL DEFAULT 0"), ("claim_owner", "TEXT"),
                              ("claimed_at", "REAL"), ("config_version", "TEXT"),
                              ("config_hash", "TEXT"), ("plan", "TEXT")):
        if name not in columns:
            db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
    return db


def claim_queued_task(db, owner: str, now: float | None = None, lease_seconds: int = 900,
                      max_attempts: int = MAX_TOTAL_ATTEMPTS):
    now = time.time() if now is None else now
    db.execute("BEGIN IMMEDIATE")
    row = db.execute("""SELECT * FROM tasks WHERE status='queued'
        OR (status='running' AND (claimed_at IS NULL OR claimed_at < ?))
        ORDER BY rowid LIMIT 1""", (now - lease_seconds,)).fetchone()
    if row is None:
        db.commit()
        return None
    attempts = row["attempts"] or 0
    if attempts >= max_attempts and row["status"] == "queued":
        db.execute("UPDATE tasks SET status='running', claim_owner=?, claimed_at=? WHERE task_id=?",
                   (owner, now, row["task_id"]))
        claimed = db.execute("SELECT * FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        db.commit()
        return claimed
    next_attempt = attempts + (1 if row["status"] == "queued" else 0)
    db.execute("UPDATE tasks SET status='running', attempts=?, claim_owner=?, claimed_at=? WHERE task_id=?",
               (next_attempt, owner, now, row["task_id"]))
    claimed = db.execute("SELECT * FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
    db.commit()
    return claimed


def release_task_claim(db, task_id: str, owner: str, requeue: bool = True):
    status = "queued" if requeue else "running"
    db.execute("UPDATE tasks SET status=?, claim_owner=NULL, claimed_at=NULL WHERE task_id=? AND claim_owner=?",
               (status, task_id, owner))


def load_pinned_catalog(task, k8s_request):
    namespace = os.environ.get("POD_NAMESPACE", "orchestrator")
    prefix = os.environ.get("AGENT_CATALOG_NAME_PREFIX", "code-factory-agent-catalog-")
    name = prefix + (task["config_hash"] or "")[:12]
    config = k8s_request("GET", f"/api/v1/namespaces/{namespace}/configmaps/{name}")
    content = config.get("data", {}).get("agent-roles.yaml", "")
    schema_content = config.get("data", {}).get("agent-roles.schema.json", "")
    runtime_content = config.get("data", {}).get("runtime-config.json", "")
    if (not content or not schema_content
                or not runtime_content
                or hashlib.sha256((content + schema_content + runtime_content).encode()).hexdigest() != task["config_hash"]):
        raise RuntimeError("task-catalog-hash-mismatch")
    catalog = yaml.safe_load(content)
    schema = json.loads(schema_content)
    validator = Draft202012Validator(schema)
    if any(validator.iter_errors(catalog)):
        raise RuntimeError("task-catalog-schema-invalid")
    if not isinstance(catalog, dict) or catalog.get("spec_version") != 1 or catalog.get("config_version") != task["config_version"]:
        raise RuntimeError("task-catalog-version-unsupported")
    roles = catalog.get("roles")
    if not isinstance(roles, dict) or set(roles) != {"coder", "judge"}:
        raise RuntimeError("task-catalog-role-set-invalid")
    allowed_tools = {"repository_read", "candidate_edit", "sandbox_validation", "candidate_review"}
    for role_id, role in roles.items():
        if (not isinstance(role, dict) or role.get("id") != role_id
                or role.get("endpoint_alias") != role_id
                or not set(role.get("tools", [])) <= allowed_tools):
            raise RuntimeError("task-catalog-role-invalid")
    if "candidate_review" in roles["coder"].get("tools", []) or "candidate_edit" in roles["judge"].get("tools", []):
        raise RuntimeError("task-catalog-capability-boundary-invalid")
    runtime = json.loads(runtime_content)
    required = {"acceptor_url", "coder_url", "coder_model", "coder_native_download", "judge_url", "judge_model",
                "threshold", "max_total_attempts", "allowed_base_branches", "reviewers", "allowed_repos",
                "repo_policies", "worker_parallelism"}
    if (not isinstance(runtime, dict) or not required <= runtime.keys()
            or runtime.get("max_total_attempts") != MAX_TOTAL_ATTEMPTS
            or type(runtime.get("threshold")) not in (float, int)
            or not 0 <= runtime["threshold"] <= 1
            or runtime.get("worker_parallelism") != 1
            or not isinstance(runtime.get("allowed_repos"), str)
            or runtime["allowed_repos"].strip() in ("", "*")):
        raise RuntimeError("task-runtime-config-invalid")
    for endpoint in (runtime["acceptor_url"], runtime["coder_url"], runtime["judge_url"]):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError("task-runtime-endpoint-invalid")
    return catalog, runtime


def dispatch_task(task, runtime, k8s_request):
    namespace = os.environ.get("POD_NAMESPACE", "orchestrator")
    template_name = os.environ.get("WORKER_JOB_TEMPLATE", "code-factory-worker")
    template = k8s_request("GET", f"/apis/batch/v1/namespaces/{namespace}/jobs/{template_name}")
    name = f"code-factory-worker-{task['task_id'][-24:]}-{task['attempts']}"
    job = copy.deepcopy(template)
    job["metadata"] = {"name": name, "namespace": namespace,
                       "labels": {"app": "orchestrator", "component": "worker",
                                  "task-id": task["task_id"], "config-hash": (task["config_hash"] or "unknown")[:63]}}
    job.pop("status", None)
    job_spec = job.setdefault("spec", {})
    job_spec.pop("suspend", None)
    job_spec.update({"parallelism": 1, "completions": 1, "backoffLimit": 0,
                     "ttlSecondsAfterFinished": int(os.environ.get("TASK_JOB_TTL_SECONDS_AFTER_FINISHED", "86400"))})
    pod_spec = job_spec["template"]["spec"]
    pod_spec["restartPolicy"] = "Never"
    pod_spec["automountServiceAccountToken"] = False
    catalog_prefix = os.environ.get("AGENT_CATALOG_NAME_PREFIX", "code-factory-agent-catalog-")
    catalog_name = catalog_prefix + (task["config_hash"] or "")[:12]
    if not task["config_hash"] or not re.fullmatch(r"[a-z0-9-]+", catalog_name):
        raise RuntimeError("task-config-pin-invalid")
    k8s_request("GET", f"/api/v1/namespaces/{namespace}/configmaps/{catalog_name}")
    for volume in pod_spec.get("volumes", []):
        if volume.get("name") == "agent-catalog":
            volume["configMap"]["name"] = catalog_name
    container = next(item for item in pod_spec["containers"] if item["name"] == "coder")
    assigned_payload = json.loads(task["payload"])
    assigned_payload["supervisor_plan"] = task["plan"] or ""
    env = [entry for entry in container.get("env", []) if entry.get("name") not in {
        "ASSIGNED_TASK_ID", "ASSIGNED_TASK_PAYLOAD", "TASK_CALLBACK_TOKEN", "TASK_REPOSITORY",
        "TASK_CREDENTIAL_EXPIRES_AT", "CODEFACTORY_AUTH_TOKEN", "REPO_GITHUB_PAT",
        "AGENT_CONFIG_VERSION", "AGENT_CONFIG_HASH", "JUDGE_URL", "JUDGE_MODEL",
        "JUDGE_NATIVE_DOWNLOAD", "JUDGE_ACCEPT_THRESHOLD", "OPENAI_API_KEY", "CODER_URL",
        "CODER_MODEL", "CODER_NATIVE_DOWNLOAD", "ACCEPTOR_URL", "ALLOWED_REPOS",
        "TASK_REPO_POLICIES", "WORKER_ROLE"}]
    env.extend([
        {"name": "ASSIGNED_TASK_ID", "value": task["task_id"]},
        {"name": "ASSIGNED_TASK_PAYLOAD", "value": json.dumps(assigned_payload, sort_keys=True)},
        {"name": "AGENT_CONFIG_VERSION", "value": task["config_version"] or "1"},
        {"name": "AGENT_CONFIG_HASH", "value": task["config_hash"]},
        {"name": "WORKER_ROLE", "value": "coder"},
        {"name": "TASK_REPOSITORY", "value": task["repo"] or ""},
        {"name": "TASK_CREDENTIAL_EXPIRES_AT", "valueFrom": {"secretKeyRef": {
            "name": task["credential_ref"], "key": "expires-at"}}},
        {"name": "REPO_GITHUB_PAT", "valueFrom": {"secretKeyRef": {
            "name": task["credential_ref"], "key": "repository-token"}}},
        {"name": "TASK_CALLBACK_TOKEN", "valueFrom": {"secretKeyRef": {
            "name": task["credential_ref"], "key": "callback-token"}}},
        {"name": "CODER_URL", "value": runtime["coder_url"]},
        {"name": "CODER_MODEL", "value": runtime["coder_model"]},
        {"name": "CODER_NATIVE_DOWNLOAD", "value": str(runtime.get("coder_native_download", False)).lower()},
        {"name": "ACCEPTOR_URL", "value": runtime["acceptor_url"]},
        {"name": "ALLOWED_REPOS", "value": runtime["allowed_repos"]},
        {"name": "TASK_REPO_POLICIES", "value": json.dumps(runtime["repo_policies"], sort_keys=True)},
    ])
    container["env"] = env
    created = k8s_request("POST", f"/apis/batch/v1/namespaces/{namespace}/jobs", job)
    return created.get("metadata", {}).get("name", name) if created else name