"""
Minimal LangGraph supervisor loop for k8s-node01.
Implements the keep-vs-defer architecture:
- one supervisor/planner
- spawned coder workers
- one judge/reviewer
- human PR gate
"""

import asyncio
import logging
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TypedDict

import httpx
from langgraph.graph import END, START, StateGraph
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
logger = logging.getLogger("supervisor-loop")

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

# --------------------------------------------------------------------------- #
# TaskState — the shared LangGraph message-passing state.
# Populated by the nodes above via state["..."] = ... / state["..."] = [ ... ].
# --------------------------------------------------------------------------- #
class TaskState(TypedDict):
    task_id: str
    issue_title: str
    repo: str
    issue_number: int
    iteration: int
    max_iterations: int
    status: str
    plan: str
    worker_outputs: list[dict]
    best_output: dict | None
    error: str | None

def build_evaluation_prompt(issue_title: str, worker_id: str, patch: str) -> str:
    """Shared template for the judge prompt, injected with worker_id/patch."""
    return f"""
You are a code reviewer. Evaluate whether this patch fixes the issue described in the GitHub issue.

Issue: {issue_title}
Worker: {worker_id}

<file_path>example.py</file_path>
<diff>
{patch}
</diff>
<context>
Here is the original code (first 100 lines):
```python
# Original code placeholder
```
</context>

Evaluate the patch based on:
1. Does it address the issue requirements?
2. Is the code correct and follows best practices?
3. Is the implementation efficient?
4. Are there any edge cases handled?
5. Is the code properly formatted and documented?

Provide a score from 0.0 to 1.0 and detailed reasoning.
"""


def make_eval_result(worker_id: str, score: float, review: str, output: dict) -> dict:
    return {"worker_id": worker_id, "score": score, "review": review, "output": output}


def make_worker_output(task_id: str, worker_id: int, status: str, extra: dict, timestamp: str) -> dict:
    return {"worker_id": worker_id, "task_id": task_id, "status": status, **extra, "timestamp": timestamp}


def passes_acceptance(state: TaskState) -> bool:
    """Whether the best output meets the ACCEPTANCE_THRESHOLD."""
    threshold = float(os.getenv("ACCEPTANCE_THRESHOLD", "0.7"))
    best = state.get("best_output")
    if best is None:
        return False
    return best.get("score", 0) >= threshold


def create_supervisor_graph():
    """Build the minimal LangGraph state machine."""
    graph = StateGraph(TaskState)
    
    # Define state transitions
    graph.add_node("plan", node_plan)
    graph.add_node("execute_workers", node_execute_workers)
    graph.add_node("evaluate", node_evaluate)
    graph.add_node("pick_best", node_pick_best)
    graph.add_node("gate", node_gate)
    
    # Edges
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "execute_workers")
    graph.add_edge("execute_workers", "evaluate")
    graph.add_edge("evaluate", "pick_best")
    graph.add_conditional_edges(
        "pick_best",
        should_retry,
        {
            "retry": "plan",
            "gate": "gate",
        }
    )
    graph.add_edge("gate", END)
    
    return graph.compile()


def node_plan(state: TaskState) -> TaskState:
    """Supervisor plans the work."""
    logger.info(f"[{state['task_id']}] Planning task: {state['issue_title']}")
    
    state["status"] = "planning"
    state["plan"] = f"""
    Task: {state['issue_title']}
    Repository: {state['repo']}
    Issue #: {state['issue_number']}
    
    Plan (iteration {state['iteration']}/{state['max_iterations']}):
    1. Spawn {os.getenv('WORKER_COUNT', '2')} parallel coder workers
    2. Each worker will attempt to fix the issue with focused edits
    3. Evaluate all outputs and pick the best one
    4. If the best passes acceptance, open a PR
    5. Otherwise, retry (up to {state['max_iterations']} iterations)
    """
    
    return state


def role_assignment(worker_count: int) -> list[str]:
    """Default role-specific mix for the worker pool (see §2.1.2).
    A mix of specialized roles rather than N identical coders."""
    roles = ["coder", "refactor", "test"]
    if worker_count <= 1:
        return [roles[0]] * worker_count
    if worker_count == 2:
        return ["coder", "test"]
    # worker_count >= 3: one refactor, one test, rest coder
    return ["refactor", "test"] + ["coder"] * max(0, worker_count - 2)


def build_role_payload(role: str, worker_id: int, issue_title: str) -> dict:
    """Role-specific spawn payload sent to the task-acceptor sidecar.
    Each role carries instructions suited to its purpose (§2.1.2)."""
    base = {"issue_title": issue_title}
    if role == "refactor":
        base["role"] = "refactor"
        base["instruction"] = (
            "Refactor the existing code for clarity, structure, and efficiency. "
            "Make minimal, focused edits; do not change behavior. Produce a patch."
        )
    elif role == "test":
        base["role"] = "test"
        base["instruction"] = (
            "Write or update focused tests that validate the issue requirements, "
            "then produce a patch that makes those tests pass."
        )
    else:
        base["role"] = "coder"
        base["instruction"] = (
            "Generate a patch that fixes the issue with narrow, test-driven edits. "
            "Produce the minimal change that addresses the requirements."
        )
    # Contract: carry the "files" key so the worker's _extract_edits picks it up.
    # Actual edits are filled in by the acceptor/coder at runtime.
    base["files"] = []
    return base


async def node_execute_workers(state: TaskState) -> TaskState:
    """Only explicit local mock execution is supported by the legacy supervisor."""
    if os.getenv("SUPERVISOR_MOCK_MODE") != "true":
        raise RuntimeError("supervisor-direct-spawn-disabled-use-repository-intake")
    worker_count = int(os.getenv("WORKER_COUNT", "2"))
    return mock_worker_executor(state, worker_count, role_assignment(worker_count))


def mock_worker_executor(state: TaskState, worker_count: int, roles: list[str] | None = None) -> dict:
    """Mock worker executor when HTTP is not available.
    Produces role-specific mock outputs (see §2.1.2)."""
    state["status"] = "executing"
    role_labels = roles or ["coder"] * worker_count
    for i, role in enumerate(role_labels):
        output = {
            "worker_id": i,
            "role": role,
            "status": "success",
            "patch": f"Mock {role} patch from worker {i}",
            "score": 0.5 + (i * 0.1),
            "timestamp": datetime.now().isoformat(),
        }
        state["worker_outputs"].append(output)
        logger.info(f"[{state['task_id']}] Mock {role} worker {i} completed (score: {output['score']})")
    return state


async def node_evaluate(state: TaskState) -> TaskState:
    """Judge evaluates all worker outputs using real inference server."""
    logger.info(f"[{state['task_id']}] Evaluating {len(state['worker_outputs'])} outputs")
    
    state["status"] = "evaluating"
    
    # Judge configuration from env vars
    judge_url = os.getenv("JUDGE_URL", "https://inference.local/v1")
    judge_model = os.getenv("JUDGE_MODEL", "qwen2.5-coder-7b-instruct-q4-km@32k")
    
    # Build evaluation prompts for each worker output
    evaluation_prompts = []
    for output in state["worker_outputs"]:
        evaluation_prompts.append({
            "worker_id": output["worker_id"],
            "prompt": build_evaluation_prompt(
                state["issue_title"], output["worker_id"], output.get("patch", ""))
        })
    
    # Evaluate each worker output using the judge (direct OpenAI-compatible chat-completions POST via httpx).
    evaluation_results = []
    for eval_prompt in evaluation_prompts:
        worker_id = eval_prompt["worker_id"]
        output = state["worker_outputs"][eval_prompt["worker_id"]]
        judge_messages = [
            {"role": "system", "content": "You are a code reviewer. Be objective and thorough."},
            {"role": "user", "content": eval_prompt["prompt"]},
        ]
        try:
            # Call the judge inference server (external OpenAI-compatible service at JUDGE_URL).
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{judge_url}/chat/completions",
                    headers={"Authorization": "Bearer " + os.getenv("OPENAI_API_KEY", "")},
                    json={
                        "model": judge_model,
                        "messages": judge_messages,
                        "temperature": 0.2,
                        "max_tokens": 1024,
                    },
                )
                if response.status_code == 200:
                    evaluation = response.json()
                    content = evaluation["choices"][0]["message"]["content"]
                    score = float(content.get("score", 0.5)) if isinstance(content, dict) else 0.5
                    review = content.get("review", "") if isinstance(content, dict) else ""
                    evaluation_results.append(make_eval_result(worker_id, score, review, output))
                    logger.info(
                        f"[{state['task_id']}] Worker {worker_id} evaluated: "
                        f"score={evaluation_results[-1]['score']:.2f}, review="
                        f"{evaluation_results[-1]['review'][:100]}...")
                else:
                    logger.warning(
                        f"[{state['task_id']}] Judge server returned {response.status_code}")
                    evaluation_results.append(make_eval_result(
                        worker_id, 0.5,
                        f"Judge evaluation failed (HTTP {response.status_code})",
                        output))
        except httpx.RequestError as e:
            logger.error(f"[{state['task_id']}] Failed to call judge: {e}")
            evaluation_results.append(make_eval_result(
                worker_id, 0.5, f"Judge evaluation failed: {e}", output))
    
    # Update worker outputs with evaluation results
    for result in evaluation_results:
        worker_id = result["worker_id"]
        for output in state["worker_outputs"]:
            if output["worker_id"] == worker_id:
                output["score"] = result["score"]
                output["review"] = result["review"]
                break
    
    # Log all evaluations
    for output in state["worker_outputs"]:
        logger.info(
            f"[{state['task_id']}] Worker {output['worker_id']} score: {output['score']}")
    
    return state


def node_pick_best(state: TaskState) -> TaskState:
    """Pick the best output."""
    logger.info(f"[{state['task_id']}] Selecting best output")
    
    if not state["worker_outputs"]:
        state["best_output"] = None
        state["error"] = "No worker outputs to evaluate"
        return state
    
    best = max(state["worker_outputs"], key=lambda x: x.get("score", 0))
    state["best_output"] = best

    threshold = float(os.getenv("ACCEPTANCE_THRESHOLD", "0.7"))
    passes = best.get("score", 0) >= threshold
    logger.info(
        f"[{state['task_id']}] Best output: worker {best['worker_id']} "
        f"(score: {best['score']}, passes: {passes})"
    )
    return state


def should_retry(state: TaskState) -> str:
    """Decide whether to retry or proceed to gate."""
    if state["best_output"] is None:
        if state["iteration"] < state["max_iterations"]:
            logger.info(f"[{state['task_id']}] No valid output, retrying")
            state["iteration"] += 1
            return "retry"
        else:
            logger.info(f"[{state['task_id']}] Max iterations reached, proceeding to gate")
            return "gate"
    
    score = state["best_output"].get("score", 0)
    if score >= float(os.getenv("ACCEPTANCE_THRESHOLD", "0.7")):
        logger.info(f"[{state['task_id']}] Output passes acceptance, proceeding to gate")
        return "gate"
    else:
        if state["iteration"] < state["max_iterations"]:
            logger.info(f"[{state['task_id']}] Output fails acceptance, retrying")
            state["iteration"] += 1
            return "retry"
        else:
            logger.info(f"[{state['task_id']}] Max iterations reached, proceeding to gate")
            return "gate"


def node_gate(state: TaskState) -> TaskState:
    """Human review gate: prepare PR."""
    logger.info(f"[{state['task_id']}] Preparing PR for human review")
    
    state["status"] = "complete"
    
    if state["best_output"]:
        logger.info(
            f"[{state['task_id']}] Opening PR with worker {state['best_output']['worker_id']} output"
        )
        # In real system: open actual PR via GitHub API
    else:
        logger.warning(f"[{state['task_id']}] No valid output, marking as failed")
        state["status"] = "failed"
        state["error"] = "No acceptable output after max iterations"
    
    return state


class SupervisorLoop:
    """Minimal supervisor loop runner."""

    def __init__(self):
        self.graph = create_supervisor_graph()
        self.logger = logger

    def _record_task_metrics(self, state: TaskState, start_time: float) -> None:
        """Record supervisor metrics for a completed task."""
        duration = max(0.0, asyncio.get_running_loop().time() - start_time)
        DURATION.observe(duration)
        status = state.get("status", "unknown")
        TASKS.labels(status=status).inc()
        ITERATIONS.inc(state.get("iteration", 1))
        ACTIVE_TASKS.set(0)
        if state.get("best_output"):
            BEST_SCORE.set(state["best_output"].get("score", 0.0))
        SUPERVISOR_STATE.set(self._state_to_value(status))
        CURRENT_ITERATION.set(state.get("iteration", 1))
        logger.info(
            f"[{state['task_id']}] Metrics: status={status} duration={duration:.2f}s "
            f"best_score={state['best_output']['score'] if state.get('best_output') else 'n/a'}")

    @staticmethod
    def _state_to_value(status: str) -> float:
        mapping = {
            "idle": 0.0,
            "planning": 1.0,
            "executing": 2.0,
            "evaluating": 3.0,
            "complete": 4.0,
            "failed": -1.0,
            "error": -2.0,
        }
        return mapping.get(status, -9.0)

    async def run_task(self, task: TaskState) -> TaskState:
        """Run one task through the graph and record metrics."""
        start_time = asyncio.get_running_loop().time()
        ACTIVE_TASKS.set(1)
        SUPERVISOR_STATE.set(self._state_to_value("planning"))
        result = await self.graph.ainvoke(task)
        self._record_task_metrics(result, start_time)
        return result

    async def run_forever(self) -> None:
        """Keep process alive and optionally run synthetic tasks for liveness."""
        interval_seconds = int(os.getenv("SUPERVISOR_IDLE_SLEEP_SECONDS", "15"))
        autorun = os.getenv("SUPERVISOR_AUTORUN", "false").lower() == "true"
        task_seq = 0
        SUPERVISOR_STATE.set(self._state_to_value("idle"))
        logger.info("Supervisor loop started (idle mode).")

        while True:
            if autorun:
                task_seq += 1
                task: TaskState = {
                    "task_id": f"bootstrap-{task_seq}",
                    "issue_title": "bootstrap health task",
                    "repo": os.getenv("ALLOWED_REPO", "*"),
                    "issue_number": task_seq,
                    "iteration": 1,
                    "max_iterations": int(os.getenv("MAX_ITERATIONS", "2")),
                    "status": "planning",
                    "plan": "",
                    "worker_outputs": [],
                    "best_output": None,
                    "error": None,
                }
                try:
                    await self.run_task(task)
                except Exception:
                    logger.exception("Supervisor task failed")
                    SUPERVISOR_STATE.set(self._state_to_value("error"))
                    TASKS.labels(status="error").inc()
                    ACTIVE_TASKS.set(0)
            else:
                SUPERVISOR_STATE.set(self._state_to_value("idle"))

            await asyncio.sleep(interval_seconds)


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
