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
from typing import TypedDict

import httpx
from langgraph.graph import END, START, StateGraph
from prometheus_client import Counter, Gauge, Histogram

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
    """Spawn and execute role-specific workers via HTTP to task-acceptor sidecar."""
    logger.info(f"[{state['task_id']}] Spawning workers for iteration {state['iteration']}")
    
    state["status"] = "executing"
    worker_count = int(os.getenv("WORKER_COUNT", "2"))
    acceptor_url = os.getenv("ACCEPTOR_URL", "http://orchestrator.local:8000")
    github_token = os.getenv("GITHUB_TOKEN")
    roles = role_assignment(worker_count)
    
    if not github_token:
        logger.warning("[" + state["task_id"] + "] GITHUB_TOKEN not set, using mock mode")
        return mock_worker_executor(state, worker_count, roles)
    
    async def _spawn_worker(i):
        """Spawn a single role-specific worker concurrently via POST /api/v1/tasks/spawn.
        Returns the accepted task_id, or None if the request was not accepted."""
        role = roles[i]
        payload = build_role_payload(role, i, state["issue_title"])
        response = await client.post(
            f"{acceptor_url}/api/v1/tasks/spawn",
            headers={
                "Authorization": f"token {github_token}",
                "Content-Type": "application/json",
            },
            json=payload or {},
        )
        if response.status_code == 202:
            task_result = response.json()
            task_id = task_result["task_id"]
            logger.info(f"[{state['task_id']}] Worker {task_id} ({role}) accepted")
            return task_id
        else:
            logger.warning(f"[{state['task_id']}] Worker {i} spawn rejected (HTTP {response.status_code})")
            return None
    
    async def _poll_worker(task_id, i):
        """Poll a spawned worker until it completes (HTTP 200) or times out.
        Runs concurrently with the other workers' poll loops."""
        try:
            wait_response = await client.get(
                f"{acceptor_url}/api/v1/tasks/{task_id}",
                headers={"Authorization": f"token {github_token}"},
            )
            if wait_response.status_code == 200:
                worker_output = wait_response.json()
                extra = {
                    "output": worker_output.get("output", ""),
                    "patch": worker_output.get("patch", ""),
                    "score": float((worker_output.get("score", 0)) or 0),
                    "review": worker_output.get("review", ""),
                }
                state["worker_outputs"].append(make_worker_output(
                    task_id, i, "success", extra, datetime.now().isoformat()))
                logger.info(f"[{state['task_id']}] Worker {i} ({roles[i]}) completed with score: {extra['score']}")
            else:
                state["worker_outputs"].append(make_worker_output(
                    task_id, i, "timeout",
                    {"error": "Worker timed out"}, datetime.now().isoformat()))
                logger.warning(f"[{state['task_id']}] Worker {task_id} timed out")
        except httpx.RequestException as e:
            state["worker_outputs"].append(make_worker_output(
                task_id, i, "error", {"error": str(e)}, datetime.now().isoformat()))
            logger.error(f"[{state['task_id']}] Failed to get worker output: {e}")
    
    async with httpx.AsyncClient(timeout=30) as client:
        # Phase 1 — spawn role-specific workers concurrently.
        task_ids = await asyncio.gather(*[_spawn_worker(i) for i in range(worker_count)])
        # Phase 2 — poll each spawned worker concurrently until it completes.
        poll_coros = [
            _poll_worker(task_id, i)
            for i, task_id in enumerate(task_ids)
            if task_id
        ]
        if poll_coros:
            await asyncio.gather(*poll_coros)
    logger.info(f"[{state['task_id']}] All {len(state['worker_outputs'])} workers completed")
    return state


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
        DURATION.labels().observe(duration)
        status = state.get("status", "unknown")
        TASKS.labels(status=status).inc()
        ITERATIONS.inc()
        ACTIVE_TASKS.dec()
        if state.get("best_output"):
            BEST_SCORE.set(state["best_output"].get("score", 0.0))
        SUPERVISOR_STATE.set(status)
        CURRENT_ITERATION.set(state.get("iteration", 1))
        logger.info(
            f"[{state['task_id']}] Metrics: status={status} duration={duration:.2f}s "
            f"best_score={state['best_output']['score'] if state.get('best_output') else 'n/a'}")
