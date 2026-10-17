"""
Unit tests for the supervisor loop helpers (supervisor.py).

These exercise the pure helper functions and the pick-best / retry decision
logic, which are deterministic and do not require a running inference server.
"""

import pytest

from supervisor import (
    build_evaluation_prompt,
    make_eval_result,
    make_worker_output,
    passes_acceptance,
    should_retry,
)


# --------------------------------------------------------------------------- #
# Evaluation helpers
# --------------------------------------------------------------------------- #


def test_build_evaluation_prompt_injects_title_and_worker():
    prompt = build_evaluation_prompt("Fix bug", "worker-7", "diff")
    assert "Fix bug" in prompt
    assert "worker-7" in prompt
    assert "diff" in prompt


def test_make_eval_result_shape():
    result = make_eval_result("worker-1", 0.8, "good", {"output": "x"})
    assert result["worker_id"] == "worker-1"
    assert result["score"] == 0.8
    assert result["review"] == "good"
    assert result["output"] == {"output": "x"}


def test_make_worker_output_shape():
    output = make_worker_output("t-1", 3, "success", {"extra": "e"}, "2026-01-01")
    assert output["task_id"] == "t-1"
    assert output["worker_id"] == 3
    assert output["status"] == "success"
    assert output["extra"] == "e"
    assert output["timestamp"] == "2026-01-01"


# --------------------------------------------------------------------------- #
# Acceptance / retry decision
# --------------------------------------------------------------------------- #


def test_passes_acceptance_above_threshold(monkeypatch):
    monkeypatch.setenv("ACCEPTANCE_THRESHOLD", "0.7")
    state = {"best_output": {"score": 0.8}, "task_id": "t-1", "iteration": 1, "max_iterations": 3}
    assert passes_acceptance(state) is True


def test_passes_acceptance_below_threshold(monkeypatch):
    monkeypatch.setenv("ACCEPTANCE_THRESHOLD", "0.7")
    state = {"best_output": {"score": 0.4}, "task_id": "t-1", "iteration": 1, "max_iterations": 3}
    assert passes_acceptance(state) is False


def test_should_retry_proceeds_to_gate_when_passing(monkeypatch):
    monkeypatch.setenv("ACCEPTANCE_THRESHOLD", "0.7")
    state = {
        "best_output": {"score": 0.9},
        "task_id": "t-1",
        "iteration": 1,
        "max_iterations": 3,
    }
    assert should_retry(state) == "gate"


def test_should_retry_retries_when_failing(monkeypatch):
    monkeypatch.setenv("ACCEPTANCE_THRESHOLD", "0.7")
    state = {
        "best_output": {"score": 0.4},
        "task_id": "t-1",
        "iteration": 1,
        "max_iterations": 3,
    }
    assert should_retry(state) == "retry"


def test_should_retry_no_output_retries(monkeypatch):
    monkeypatch.setenv("ACCEPTANCE_THRESHOLD", "0.7")
    state = {
        "best_output": None,
        "task_id": "t-1",
        "iteration": 1,
        "max_iterations": 3,
    }
    assert should_retry(state) == "retry"


def test_should_retry_no_output_gates_at_max(monkeypatch):
    monkeypatch.setenv("ACCEPTANCE_THRESHOLD", "0.7")
    state = {
        "best_output": None,
        "task_id": "t-1",
        "iteration": 3,
        "max_iterations": 3,
    }
    assert should_retry(state) == "gate"


def test_should_retry_retries_at_max_iterations(monkeypatch):
    monkeypatch.setenv("ACCEPTANCE_THRESHOLD", "0.7")
    state = {
        "best_output": {"score": 0.4},
        "task_id": "t-1",
        "task_id": "t-1",
        "iteration": 3,
        "max_iterations": 3,
    }
    assert should_retry(state) == "gate"


def test_should_retry_passing_at_max_iterations(monkeypatch):
    monkeypatch.setenv("ACCEPTANCE_THRESHOLD", "0.7")
    state = {
        "best_output": {"score": 0.9},
        "task_id": "t-1",
        "iteration": 3,
        "max_iterations": 3,
    }
    assert should_retry(state) == "gate"
