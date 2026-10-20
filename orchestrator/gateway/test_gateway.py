"""
Unit tests for the gateway repo-allowlist helpers (server.py).

These exercise the _parse_allowed_repos helper and the repo-validation logic,
which are deterministic and do not require a running HTTP server.
"""

import copy
import io
import json
import os
from unittest.mock import Mock

import pytest
import server

# --------------------------------------------------------------------------- #
# _parse_allowed_repos helper
# --------------------------------------------------------------------------- #


def test_wildcard_means_any_repo():
    assert server._parse_allowed_repos("*") is None
    assert server._parse_allowed_repos("*") == server._parse_allowed_repos("*")


def test_single_repo_whitelist():
    result = server._parse_allowed_repos("org/repo1")
    assert result == {"org/repo1"}


def test_multiple_comma_separated_repos():
    result = server._parse_allowed_repos("org/a,org/b, other/c ")
    assert result == {"org/a", "org/b", "other/c"}


def test_empty_and_blank_fallbacks():
    assert server._parse_allowed_repos("") is None
    assert server._parse_allowed_repos("   ") is None
    assert server._parse_allowed_repos(None) is None


def test_legacy_allowed_repo_variable():
    # Backward-compatible: ALLOWED_REPO is read when ALLOWED_REPOS is unset.
    os.environ.pop("ALLOWED_REPOS", None)
    os.environ["ALLOWED_REPO"] = "UtopikLab/code-factory"
    del server.ALLOWED_REPOS_RAW
    del server.ALLOWED_REPOS
    server.ALLOWED_REPOS_RAW = os.environ.get(
        "ALLOWED_REPOS", os.environ.get("ALLOWED_REPO", "*")
    )
    server.ALLOWED_REPOS = server._parse_allowed_repos(server.ALLOWED_REPOS_RAW)
    assert server.ALLOWED_REPOS == {"UtopikLab/code-factory"}


# --------------------------------------------------------------------------- #
# Repo validation (mirrors the do_POST branch)
# --------------------------------------------------------------------------- #


def _repo_allowed(repo, allowed):
    if allowed is None:
        return True  # "*" -> accept any repo
    return allowed is not None and (repo in allowed)


def test_any_repo_policy_accepts_all():
    assert _repo_allowed("anything/else", None) is True


def test_whitelist_rejects_unknown():
    allowed = {"org/a"}
    assert _repo_allowed("org/a", allowed) is True
    assert _repo_allowed("org/b", allowed) is False


def test_single_repo_whitelist_rejects_others():
    allowed = {"UtopikLab/code-factory"}
    assert _repo_allowed("UtopikLab/code-factory", allowed) is True
    assert _repo_allowed("other/repo", allowed) is False


def test_multi_repo_whitelist():
    allowed = {"org/a", "org/b"}
    assert _repo_allowed("org/a", allowed) is True
    assert _repo_allowed("org/b", allowed) is True
    assert _repo_allowed("org/c", allowed) is False


# --------------------------------------------------------------------------- #
# External payload detection + Job name helper
# --------------------------------------------------------------------------- #


def test_external_workflow_payload_detected():
    payload = {
        "name": "workflow_dispatch",
        "ref": "refs/heads/main",
        "payload": {"issue_number": 123},
    }
    assert server._is_external_workflow_payload(payload) is True


def test_worker_internal_payload_is_not_external():
    payload = {
        "worker_id": "worker-1",
        "payload": {"issue_number": 123},
    }
    assert server._is_external_workflow_payload(payload) is False


def test_payload_missing_required_external_fields_is_not_external():
    payload = {"payload": {"issue_number": 123}}
    assert server._is_external_workflow_payload(payload) is False


def test_build_worker_job_name_sanitized_and_limited(monkeypatch):
    monkeypatch.setattr(server.time, "time", lambda: 1700000000)
    name = server._build_worker_job_name("Task ID With Spaces_and_SYMBOLS___" + ("x" * 80))

    assert name.startswith("orchestrator-worker-")
    assert name.endswith("-1700000000")
    assert len(name) <= 63
    assert "_" not in name
    assert " " not in name


@pytest.mark.parametrize("label_location", ["job", "pod", "both"])
def test_clone_job_strips_generated_labels(label_location):
    generated_labels = {
        "controller-uid": "old-controller",
        "batch.kubernetes.io/controller-uid": "old-controller",
        "job-name": "old-job",
        "batch.kubernetes.io/job-name": "old-job",
    }
    job_labels = {"app": "worker", "task-id": "old-task", "triggered-by": "old-trigger"}
    pod_labels = {"component": "worker-pod"}
    if label_location in ("job", "both"):
        job_labels.update(generated_labels)
    if label_location in ("pod", "both"):
        pod_labels.update(generated_labels)
    template = {
        "metadata": {"labels": job_labels},
        "spec": {"template": {"metadata": {"labels": pod_labels}}},
    }
    original = copy.deepcopy(template)

    cloned = server._clone_job_from_template(template, "orchestrator", "new-job", 123)

    expected_labels = {"app": "worker", "task-id": "123", "triggered-by": "gateway"}
    assert cloned["metadata"]["labels"] == expected_labels
    assert cloned["spec"]["template"]["metadata"]["labels"] == {
        "component": "worker-pod", **expected_labels,
    }
    assert template == original


def test_clone_job_passes_assigned_payload_to_one_worker():
    payload = {
        "name": "workflow_dispatch", "ref": "refs/heads/main", "repo": "org/repo",
        "payload": {"issue_number": 2, "edits": [{"path": "a.py", "content": "x=1"}]},
    }
    template = {
        "spec": {
            "parallelism": 2, "completions": 8, "backoffLimit": 4,
            "template": {"spec": {"containers": [{
                "name": "coder", "env": [{"name": "GITHUB_TOKEN", "valueFrom": {
                    "secretKeyRef": {"name": "secrets", "key": "github-token"},
                }}],
            }]}},
        },
    }
    original = copy.deepcopy(template)
    cloned = server._clone_job_from_template(template, "orchestrator", "new-job", 2, payload)
    spec = cloned["spec"]
    assert (spec["parallelism"], spec["completions"], spec["backoffLimit"]) == (1, 1, 0)
    env = spec["template"]["spec"]["containers"][0]["env"]
    assert env[0] == original["spec"]["template"]["spec"]["containers"][0]["env"][0]
    values = {entry["name"]: entry.get("value") for entry in env}
    assert values["ASSIGNED_TASK_ID"] == "2"
    assert json.loads(values["ASSIGNED_TASK_PAYLOAD"]) == payload
    assert template == original


@pytest.mark.parametrize("payload", [
    [], None, {"payload": []}, {"payload": None},
    {"name": "orchestrator-agent", "ref": "main", "payload": {"issue_number": None}},
    {"name": "orchestrator-agent", "ref": "main", "payload": {"issue_number": True}},
    {"name": "orchestrator-agent", "ref": "main", "payload": {"issue_number": "unknown"}},
    {"name": "orchestrator-agent", "ref": "main", "payload": {"issue_number": "../2"}},
    {"name": "orchestrator-agent", "ref": "main", "task_id": "", "payload": {"issue_number": 2}},
])
def test_gateway_rejects_malformed_assignment_before_job_creation(monkeypatch, payload):
    create = Mock(side_effect=AssertionError("No cluster calls expected"))
    monkeypatch.setattr(server, "_create_worker_job", create)
    monkeypatch.setattr(server, "TOKEN", "test-token")
    body = json.dumps(payload).encode()
    handler = server.Handler.__new__(server.Handler)
    handler.headers = {"Authorization": "token test-token", "Content-Length": str(len(body))}
    handler.path = "/api/v1/tasks/spawn"
    handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
    handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
    handler.do_POST()
    handler.send_response.assert_called_once_with(400)
    assert "error" in json.loads(handler.wfile.getvalue())
    create.assert_not_called()
