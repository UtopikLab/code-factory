#!/usr/bin/env python3
"""
Minimal LangGraph supervisor loop for k8s-node01.
Implements the keep-vs-defer architecture:
- one supervisor/planner
- spawned coder workers
- one judge/reviewer
- human PR gate
"""

import os
import json
import asyncio
import logging
from typing import TypedDict, Annotated
from datetime import datetime

try:
    from langgraph.graph import StateGraph, START, END
    from langchain_openai import ChatOpenAI
except ImportError:
    print("Warning: LangGraph not available, using mock mode")
    StateGraph = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
logger = logging.getLogger("supervisor-loop")


class TaskState(TypedDict):
    """The mutable state carried through the loop."""
    task_id: str
    issue_number: int
    issue_title: str
    issue_url: str
    repo: str
    status: str  # "received", "planning", "executing", "evaluating", "complete", "failed"
    plan: str
    iteration: int
    max_iterations: int
    worker_outputs: list[dict]
    best_output: dict | None
    error: str | None
    created_at: str


def create_supervisor_graph():
    """Build the minimal LangGraph state machine."""
    
    if StateGraph is None:
        logger.warning("LangGraph not available, running in mock mode")
        return None
    
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


def node_execute_workers(state: TaskState) -> TaskState:
    """Spawn and execute coder workers."""
    logger.info(f"[{state['task_id']}] Spawning workers for iteration {state['iteration']}")
    
    state["status"] = "executing"
    worker_count = int(os.getenv("WORKER_COUNT", "2"))
    
    # Mock worker outputs (in real system, these would be K8s Job outputs)
    for i in range(worker_count):
        output = {
            "worker_id": i,
            "status": "success",
            "patch": f"Mock patch from worker {i}",
            "score": 0.5 + (i * 0.1),  # Mock quality score
            "timestamp": datetime.now().isoformat(),
        }
        state["worker_outputs"].append(output)
        logger.info(f"[{state['task_id']}] Worker {i} completed (score: {output['score']})")
    
    return state


def node_evaluate(state: TaskState) -> TaskState:
    """Judge evaluates all worker outputs."""
    logger.info(f"[{state['task_id']}] Evaluating {len(state['worker_outputs'])} outputs")
    
    state["status"] = "evaluating"
    
    # Mock evaluation: score based on worker_id (in real system, judge would score)
    for output in state["worker_outputs"]:
        logger.info(
            f"[{state['task_id']}] Worker {output['worker_id']} score: {output['score']}"
        )
    
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
    
    acceptance_threshold = float(os.getenv("ACCEPTANCE_THRESHOLD", "0.7"))
    passes_acceptance = best.get("score", 0) >= acceptance_threshold
    
    logger.info(
        f"[{state['task_id']}] Best output: worker {best['worker_id']} "
        f"(score: {best['score']}, passes: {passes_acceptance})"
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
    
    acceptance_threshold = float(os.getenv("ACCEPTANCE_THRESHOLD", "0.7"))
    if state["best_output"].get("score", 0) >= acceptance_threshold:
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
    
    async def process_task(self, task_payload: dict) -> TaskState:
        """Process a single task through the loop."""
        
        max_iterations = int(os.getenv("MAX_ITERATIONS", "8"))
        
        state: TaskState = {
            "task_id": task_payload.get("task_id", "unknown"),
            "issue_number": task_payload.get("issue_number", 0),
            "issue_title": task_payload.get("issue_title", "Unknown task"),
            "issue_url": task_payload.get("issue_url", ""),
            "repo": task_payload.get("repo", "unknown"),
            "status": "received",
            "plan": "",
            "iteration": 1,
            "max_iterations": max_iterations,
            "worker_outputs": [],
            "best_output": None,
            "error": None,
            "created_at": datetime.now().isoformat(),
        }
        
        self.logger.info(f"[{state['task_id']}] Task received: {state['issue_title']}")
        
        if self.graph:
            # Run through LangGraph
            try:
                final_state = self.graph.invoke(state)
                self.logger.info(f"[{state['task_id']}] Task complete, status: {final_state['status']}")
                return final_state
            except Exception as e:
                self.logger.error(f"[{state['task_id']}] Error in graph: {e}")
                state["status"] = "failed"
                state["error"] = str(e)
                return state
        else:
            # Mock mode: run nodes sequentially
            self.logger.info(f"[{state['task_id']}] Running in mock mode (LangGraph not available)")
            state = node_plan(state)
            state = node_execute_workers(state)
            state = node_evaluate(state)
            state = node_pick_best(state)
            state = node_gate(state)
            return state


async def run_loop():
    """Main loop runner - accept tasks and process them."""
    supervisor = SupervisorLoop()
    logger.info("Supervisor loop initialized and listening for tasks")
    
    # For now, just keep the loop alive
    # In production, this would receive tasks from the task-acceptor sidecar
    while True:
        await asyncio.sleep(60)
        logger.info("Supervisor loop alive, waiting for tasks...")


if __name__ == "__main__":
    logger.info("Starting supervisor loop...")
    asyncio.run(run_loop())
