"""LangGraph state model and task-planning/evaluation transitions."""

from datetime import datetime
from typing import TypedDict


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


def passes_acceptance(state, supervisor) -> bool:
    threshold = float(supervisor.os.getenv("ACCEPTANCE_THRESHOLD", "0.7"))
    best = state.get("best_output")
    return best is not None and best.get("score", 0) >= threshold


def create_supervisor_graph(supervisor):
    graph = supervisor.StateGraph(supervisor.TaskState)
    graph.add_node("plan", supervisor.node_plan)
    graph.add_node("execute_workers", supervisor.node_execute_workers)
    graph.add_node("evaluate", supervisor.node_evaluate)
    graph.add_node("pick_best", supervisor.node_pick_best)
    graph.add_node("gate", supervisor.node_gate)
    graph.add_edge(supervisor.START, "plan")
    graph.add_edge("plan", "execute_workers")
    graph.add_edge("execute_workers", "evaluate")
    graph.add_edge("evaluate", "pick_best")
    graph.add_conditional_edges("pick_best", supervisor.should_retry,
                                {"retry": "plan", "gate": "gate"})
    graph.add_edge("gate", supervisor.END)
    return graph.compile()


def node_plan(state, supervisor):
    supervisor.logger.info(f"[{state['task_id']}] Planning task: {state['issue_title']}")
    state["status"] = "planning"
    state["plan"] = f"""
    Task: {state['issue_title']}
    Repository: {state['repo']}
    Issue #: {state['issue_number']}

    Plan (iteration {state['iteration']}/{state['max_iterations']}):
    1. Spawn {supervisor.os.getenv('WORKER_COUNT', '2')} parallel coder workers
    2. Each worker will attempt to fix the issue with focused edits
    3. Evaluate all outputs and pick the best one
    4. If the best passes acceptance, open a PR
    5. Otherwise, retry (up to {state['max_iterations']} iterations)
    """
    return state


def role_assignment(worker_count: int) -> list[str]:
    roles = ["coder", "refactor", "test"]
    if worker_count <= 1:
        return [roles[0]] * worker_count
    if worker_count == 2:
        return ["coder", "test"]
    return ["refactor", "test"] + ["coder"] * max(0, worker_count - 2)


def build_role_payload(role: str, worker_id: int, issue_title: str) -> dict:
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
    base["files"] = []
    return base


async def node_execute_workers(state, supervisor):
    if supervisor.os.getenv("SUPERVISOR_MOCK_MODE") != "true":
        raise RuntimeError("supervisor-direct-spawn-disabled-use-repository-intake")
    worker_count = int(supervisor.os.getenv("WORKER_COUNT", "2"))
    return mock_worker_executor(state, worker_count, role_assignment(worker_count), supervisor)


def mock_worker_executor(state, worker_count: int, roles, supervisor):
    state["status"] = "executing"
    role_labels = roles or ["coder"] * worker_count
    for worker_id, role in enumerate(role_labels):
        output = {"worker_id": worker_id, "role": role, "status": "success",
                  "patch": f"Mock {role} patch from worker {worker_id}",
                  "score": 0.5 + (worker_id * 0.1),
                      "timestamp": datetime.now().isoformat()}
        state["worker_outputs"].append(output)
        supervisor.logger.info(f"[{state['task_id']}] Mock {role} worker {worker_id} completed (score: {output['score']})")
    return state


async def node_evaluate(state, supervisor):
    supervisor.logger.info(f"[{state['task_id']}] Evaluating {len(state['worker_outputs'])} outputs")
    state["status"] = "evaluating"
    judge_url = supervisor.os.getenv("JUDGE_URL", "https://inference.local/v1")
    judge_model = supervisor.os.getenv("JUDGE_MODEL", "qwen2.5-coder-7b-instruct-q4-km@32k")
    evaluation_prompts = [{"worker_id": output["worker_id"],
                           "prompt": build_evaluation_prompt(state["issue_title"], output["worker_id"], output.get("patch", ""))}
                          for output in state["worker_outputs"]]
    evaluation_results = []
    for eval_prompt in evaluation_prompts:
        worker_id = eval_prompt["worker_id"]
        output = state["worker_outputs"][eval_prompt["worker_id"]]
        judge_messages = [{"role": "system", "content": "You are a code reviewer. Be objective and thorough."},
                          {"role": "user", "content": eval_prompt["prompt"]}]
        try:
            async with supervisor.httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{judge_url}/chat/completions",
                    headers={"Authorization": "Bearer " + supervisor.os.getenv("OPENAI_API_KEY", "")},
                    json={"model": judge_model, "messages": judge_messages, "temperature": 0.2, "max_tokens": 1024})
                if response.status_code == 200:
                    evaluation = response.json()
                    content = evaluation["choices"][0]["message"]["content"]
                    score = float(content.get("score", 0.5)) if isinstance(content, dict) else 0.5
                    review = content.get("review", "") if isinstance(content, dict) else ""
                    evaluation_results.append(make_eval_result(worker_id, score, review, output))
                    supervisor.logger.info(f"[{state['task_id']}] Worker {worker_id} evaluated: "
                                           f"score={evaluation_results[-1]['score']:.2f}, review="
                                           f"{evaluation_results[-1]['review'][:100]}...")
                else:
                    supervisor.logger.warning(f"[{state['task_id']}] Judge server returned {response.status_code}")
                    evaluation_results.append(make_eval_result(worker_id, 0.5,
                                                               f"Judge evaluation failed (HTTP {response.status_code})", output))
        except supervisor.httpx.RequestError as error:
            supervisor.logger.error(f"[{state['task_id']}] Failed to call judge: {error}")
            evaluation_results.append(make_eval_result(worker_id, 0.5, f"Judge evaluation failed: {error}", output))
    for result in evaluation_results:
        for output in state["worker_outputs"]:
            if output["worker_id"] == result["worker_id"]:
                output["score"] = result["score"]
                output["review"] = result["review"]
                break
    for output in state["worker_outputs"]:
        supervisor.logger.info(f"[{state['task_id']}] Worker {output['worker_id']} score: {output['score']}")
    return state


def node_pick_best(state, supervisor):
    supervisor.logger.info(f"[{state['task_id']}] Selecting best output")
    if not state["worker_outputs"]:
        state["best_output"] = None
        state["error"] = "No worker outputs to evaluate"
        return state
    best = max(state["worker_outputs"], key=lambda output: output.get("score", 0))
    state["best_output"] = best
    threshold = float(supervisor.os.getenv("ACCEPTANCE_THRESHOLD", "0.7"))
    passes = best.get("score", 0) >= threshold
    supervisor.logger.info(f"[{state['task_id']}] Best output: worker {best['worker_id']} "
                           f"(score: {best['score']}, passes: {passes})")
    return state


def should_retry(state, supervisor) -> str:
    if state["best_output"] is None:
        if state["iteration"] < state["max_iterations"]:
            supervisor.logger.info(f"[{state['task_id']}] No valid output, retrying")
            state["iteration"] += 1
            return "retry"
        supervisor.logger.info(f"[{state['task_id']}] Max iterations reached, proceeding to gate")
        return "gate"
    score = state["best_output"].get("score", 0)
    if score >= float(supervisor.os.getenv("ACCEPTANCE_THRESHOLD", "0.7")):
        supervisor.logger.info(f"[{state['task_id']}] Output passes acceptance, proceeding to gate")
        return "gate"
    if state["iteration"] < state["max_iterations"]:
        supervisor.logger.info(f"[{state['task_id']}] Output fails acceptance, retrying")
        state["iteration"] += 1
        return "retry"
    supervisor.logger.info(f"[{state['task_id']}] Max iterations reached, proceeding to gate")
    return "gate"


def node_gate(state, supervisor):
    supervisor.logger.info(f"[{state['task_id']}] Preparing PR for human review")
    state["status"] = "complete"
    if state["best_output"]:
        supervisor.logger.info(f"[{state['task_id']}] Opening PR with worker {state['best_output']['worker_id']} output")
    else:
        supervisor.logger.warning(f"[{state['task_id']}] No valid output, marking as failed")
        state["status"] = "failed"
        state["error"] = "No acceptable output after max iterations"
    return state