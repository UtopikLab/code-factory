"""
Minimal LangGraph supervisor loop for k8s-node01.
Implements the keep-vs-defer architecture:
- one supervisor/planner
- spawned coder workers
- one judge/reviewer
- human PR gate
"""

import asyncio
import json
import logging
import os
import ssl
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import candidate_workflow as _candidate_workflow
import httpx
import task_graph as _task_graph
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from supervisor_runtime import SupervisorLoop as _SupervisorRuntime
from task_runtime import (
    claim_queued_task as _claim_queued_task,
)
from task_runtime import (
    dispatch_task as _runtime_dispatch_task,
)
from task_runtime import (
    load_pinned_catalog as _runtime_load_pinned_catalog,
)
from task_runtime import (
    release_task_claim as _release_task_claim,
)
from task_runtime import (
    task_database as _task_database,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
logger = logging.getLogger("supervisor-loop")

MAX_TOTAL_ATTEMPTS = 8
TASK_DB_PATH = os.environ.get("TASK_DB_PATH", "/data/tasks/tasks.sqlite3")


def task_database(path: str | None = None):
    return _task_database(path or TASK_DB_PATH)


def claim_queued_task(db, owner: str, now: float | None = None, lease_seconds: int = 900):
    return _claim_queued_task(db, owner, now, lease_seconds, MAX_TOTAL_ATTEMPTS)


def release_task_claim(db, task_id: str, owner: str, requeue: bool = True):
    return _release_task_claim(db, task_id, owner, requeue)


def _load_pinned_catalog(task):
    return _runtime_load_pinned_catalog(task, _k8s_request)


def _k8s_request(method: str, path: str, body: dict | None = None):
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    if not host:
        raise RuntimeError("kubernetes-api-unavailable")
    token_path = os.environ.get("KUBERNETES_TOKEN_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/token")
    with open(token_path, encoding="utf-8") as token_file:
        token = token_file.read().strip()
    request = urllib.request.Request(
        f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT_HTTPS', '443')}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "Content-Type": "application/json"}, method=method)
    context = ssl.create_default_context(cafile=os.environ.get(
        "KUBERNETES_CA_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"))
    try:
        with urllib.request.urlopen(request, context=context, timeout=20) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        if error.code == 409 and method == "POST":
            return None
        raise RuntimeError(f"kubernetes-api-http-{error.code}") from None


def _dispatch_task(task, runtime):
    return _runtime_dispatch_task(task, runtime, _k8s_request)


def _github_request(method: str, repo: str, path: str, body: dict | None = None):
    token = os.environ.get("PR_GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("pr-publication-credential-required")
    request = urllib.request.Request("https://api.github.com/repos/" + repo + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + token,
                                              "Accept": "application/vnd.github+json",
                                              "Content-Type": "application/json"}, method=method)
    try:
        opener = urllib.request.build_opener(RejectRedirect())
        with opener.open(request, timeout=30) as response:
            raw = response.read(1048577)
            if len(raw) > 1048576:
                raise RuntimeError("github-response-too-large")
            return json.loads(raw or b"{}")
    except urllib.error.HTTPError as error:
        if error.code == 404 and method == "GET":
            return None
        raise RuntimeError(f"github-api-http-{error.code}") from None


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("github-redirect-rejected")


def _verified_candidate_patch(task, candidate):
    return _candidate_workflow.verified_candidate_patch(task, candidate, _github_request)


async def _judge_candidate(task, candidate, runtime=None):
    return await _candidate_workflow.judge_candidate(task, candidate, runtime, httpx)


async def _plan_task(task, catalog, runtime=None):
    return await _candidate_workflow.plan_task(task, catalog, runtime, MAX_TOTAL_ATTEMPTS, httpx)


def _publish_candidate(task, candidate, runtime=None):
    return _candidate_workflow.publish_candidate(task, candidate, runtime, _github_request)


def _complete_via_gateway(task_id: str, status: str, runtime=None, **details):
    return _candidate_workflow.complete_via_gateway(
        task_id, status, runtime, details, urllib.request.urlopen)

# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
# Application-level observability for the supervisor LangGraph loop. Scraped by
# the central Prometheus server on port 9000 (kept-vs-defer: no second stack).
#   * orchestrator_tasks_total  — total tasks processed (counter)
#   * orchestrator_tasks_by_status{status}  — counter per lifecycle status
#   * orchestrator_iterations_total  — total loop iterations across all tasks
#   * orchestrator_best_score  — gauge of the current best evaluation score
#   * orchestrator_current_iteration  — gauge of the supervisor's current iteration
#   * orchestrator_supervisor_state  — gauge of the supervisor's status string
#   * orchestrator_duration_seconds  — histogram of loop duration per task
#   * orchestrator_active_tasks  — gauge of in-flight tasks
# --------------------------------------------------------------------------- #
TASKS = Counter("orchestrator_tasks_total", "Total tasks processed by the supervisor loop.", ["status"])
ITERATIONS = Counter("orchestrator_iterations_total", "Total loop iterations across all tasks.")
BEST_SCORE = Gauge("orchestrator_best_score", "Current best evaluation score of the best worker output.")
CURRENT_ITERATION = Gauge("orchestrator_current_iteration", "Supervisor's current iteration for the active task.")
SUPERVISOR_STATE = Gauge("orchestrator_supervisor_state", "Supervisor's current lifecycle status string.")
DURATION = Histogram("orchestrator_duration_seconds", "Duration in seconds of a single task through the loop.")
ACTIVE_TASKS = Gauge("orchestrator_active_tasks", "Number of tasks currently in-flight.")
# --------------------------------------------------------------------------- #

TaskState = _task_graph.TaskState

def build_evaluation_prompt(issue_title: str, worker_id: str, patch: str) -> str:
    return _task_graph.build_evaluation_prompt(issue_title, worker_id, patch)


def make_eval_result(worker_id: str, score: float, review: str, output: dict) -> dict:
    return _task_graph.make_eval_result(worker_id, score, review, output)


def make_worker_output(task_id: str, worker_id: int, status: str, extra: dict, timestamp: str) -> dict:
    return _task_graph.make_worker_output(task_id, worker_id, status, extra, timestamp)


def passes_acceptance(state: TaskState) -> bool:
    return _task_graph.passes_acceptance(state, sys.modules[__name__])


def create_supervisor_graph():
    return _task_graph.create_supervisor_graph(sys.modules[__name__])


def node_plan(state: TaskState) -> TaskState:
    return _task_graph.node_plan(state, sys.modules[__name__])


def role_assignment(worker_count: int) -> list[str]:
    return _task_graph.role_assignment(worker_count)


def build_role_payload(role: str, worker_id: int, issue_title: str) -> dict:
    return _task_graph.build_role_payload(role, worker_id, issue_title)


async def node_execute_workers(state: TaskState) -> TaskState:
    return await _task_graph.node_execute_workers(state, sys.modules[__name__])


def mock_worker_executor(state: TaskState, worker_count: int, roles: list[str] | None = None) -> dict:
    return _task_graph.mock_worker_executor(state, worker_count, roles, sys.modules[__name__])


async def node_evaluate(state: TaskState) -> TaskState:
    return await _task_graph.node_evaluate(state, sys.modules[__name__])


def node_pick_best(state: TaskState) -> TaskState:
    return _task_graph.node_pick_best(state, sys.modules[__name__])


def should_retry(state: TaskState) -> str:
    return _task_graph.should_retry(state, sys.modules[__name__])


def node_gate(state: TaskState) -> TaskState:
    return _task_graph.node_gate(state, sys.modules[__name__])


class SupervisorLoop(_SupervisorRuntime):
    def __init__(self):
        super().__init__(sys.modules[__name__])


class _ProbeHandler(BaseHTTPRequestHandler):
    """Minimal HTTP endpoints for readiness/liveness and Prometheus scrape."""

    def do_GET(self):
        if self.path == "/healthz" or self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return

        if self.path == "/metrics":
            data = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"not found\n")

    def log_message(self, fmt, *args):
        logger.info("probe_http: " + fmt, *args)


def _start_probe_server() -> ThreadingHTTPServer:
    host = os.getenv("SUPERVISOR_BIND_HOST", "0.0.0.0")
    port = int(os.getenv("SUPERVISOR_METRICS_PORT", "9000"))
    server = ThreadingHTTPServer((host, port), _ProbeHandler)
    logger.info("Supervisor probe server listening on %s:%s", host, port)
    asyncio.get_running_loop().run_in_executor(None, server.serve_forever)
    return server


async def _main_async() -> None:
    server = _start_probe_server()
    loop = SupervisorLoop()
    try:
        await loop.run_forever()
    finally:
        server.shutdown()
        server.server_close()


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
