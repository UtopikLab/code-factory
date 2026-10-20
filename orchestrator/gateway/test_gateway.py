"""
Unit tests for the gateway repo-allowlist helpers (server.py).

These exercise the _parse_allowed_repos helper and the repo-validation logic,
which are deterministic and do not require a running HTTP server.
"""

import copy
import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import server


@pytest.fixture
def task_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(server, "TOKEN", "test-token")
    monkeypatch.setattr(server, "ALLOWED_REPOS", {"org/repo", "org/other"})
    create = Mock(return_value={"name": "assigned-worker", "uid": "uid"})
    monkeypatch.setattr(server, "_create_worker_job", create)
    github = Mock(side_effect=RuntimeError("GitHub unavailable"))
    monkeypatch.setattr(server, "_github", github)
    return create, github


def task_request(method, path, payload=None, token="test-token"):
    body = json.dumps(payload).encode()
    handler = server.Handler.__new__(server.Handler)
    handler.command = method
    handler.headers = {"Authorization": f"token {token}", "Content-Length": str(len(body))}
    handler.path = path
    handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
    handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
    getattr(handler, f"do_{method}")()
    return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())


def issue_payload(repo="org/repo"):
    return {"name": "drain", "ref": "refs/heads/main", "payload": {"repo": repo, "issue_number": 2}}


def test_durable_repo_scoped_tasks_and_report_retry(task_gateway):
    create, github = task_gateway
    code, spawned = task_request("POST", "/api/v1/tasks/spawn", issue_payload())
    assert code == 202
    task_id = spawned["task_id"]
    assert task_id.startswith("issue-")
    assigned = create.call_args.args[3]
    assert assigned["task_id"] == task_id
    assert assigned["payload"] == issue_payload()["payload"]
    assert task_request("GET", f"/api/v1/tasks/{task_id}")[1]["status"] == "running"
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "completed",
              "patch": "real patch", "pr_url": "https://github.com/org/repo/pull/3"}
    path = f"/api/v1/tasks/{task_id}"
    code, report = task_request("POST", path, result)
    assert code == 200 and report["lifecycle"] == "pending"
    assert task_request("GET", path)[1]["result"] == result
    calls = []

    def api(method, path, body=None):
        calls.append((method, path, body))
        if path.endswith("/issues/2"):
            return {"number": 2, "html_url": "https://github.com/org/repo/issues/2"}
        if method == "GET" and "/labels" in path:
            return [{"name": "queued"}]
        if method == "GET" and "/comments" in path:
            return []
        return {}

    github.side_effect = api
    assert task_request("POST", path, result)[1]["lifecycle"] == "synced"
    assert ("DELETE", "/repos/org/repo/issues/2/labels/queued", None) in calls
    assert any(body and body.get("labels") == ["done"] for _, _, body in calls)
    assert any(body and result["pr_url"] in body.get("body", "") for _, _, body in calls)
    count = len(calls)
    assert task_request("POST", path, result)[0] == 200
    assert len(calls) == count
    assert task_request("GET", "/api/v1/tasks/2?repo=org%2Frepo")[1]["task_id"] == task_id
    assert task_request("GET", "/api/v1/tasks/2")[0] == 404
    assert task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["replayed"]
    assert create.call_count == 1
    assert task_request("POST", path, {**result, "status": "failed"})[0] == 409
    other = task_request("POST", "/api/v1/tasks/spawn", issue_payload("org/other"))[1]
    assert other["task_id"] != task_id
    assert task_request("POST", path, {**result, "repo": "org/other"})[0] == 400
    assert task_request("GET", path, token="wrong")[0] == 401


def test_concurrent_duplicate_spawn_and_reports(task_gateway, monkeypatch):
    create, _ = task_gateway
    sync = Mock()
    monkeypatch.setattr(server, "_sync_lifecycle", sync)
    def spawn(_):
        return task_request("POST", "/api/v1/tasks/spawn", issue_payload())
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(spawn, range(4)))
    task_id = responses[0][1]["task_id"]
    assert {response[1]["task_id"] for response in responses} == {task_id}
    assert create.call_count == 1
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "validation-failed"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(lambda _: task_request("POST", f"/api/v1/tasks/{task_id}", result), range(4)))
    assert all(code == 200 for code, _ in reports)
    assert sync.call_count == 1


def test_spawn_failure_is_not_accepted(task_gateway):
    create, _ = task_gateway
    create.side_effect = RuntimeError("cluster unavailable")
    code, body = task_request("POST", "/api/v1/tasks/spawn", issue_payload())
    assert code == 500
    task = task_request("GET", f"/api/v1/tasks/{body['task_id']}")[1]
    assert task["status"] == "failed" and task["result"]["error"] == "job-create-failed"
    assert task_request("POST", "/api/v1/tasks/spawn", issue_payload())[0] == 409
    assert create.call_count == 1


def test_task_storage_survives_new_process(task_gateway):
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "validation-failed"}
    assert task_request("POST", f"/api/v1/tasks/{task_id}", result)[0] == 200
    output = subprocess.check_output([
        sys.executable, "-c",
        ("import json, sqlite3, sys; db = sqlite3.connect(sys.argv[1]); "
         "print(json.dumps(db.execute('SELECT status, result FROM tasks WHERE task_id=?', (sys.argv[2],)).fetchone()))"),
        os.environ["TASK_DB_PATH"], task_id,
    ], text=True)
    status, saved = json.loads(output)
    assert status == "failed" and json.loads(saved) == result


@pytest.mark.parametrize("bad", [
    {"repo": "org/repo", "payload": {"repo": "org/other", "issue_number": 2}},
    {"repo": "../repo", "issue_number": 2},
    {"repo": "org/repo", "issue_number": True},
    {"repo": "org/repo", "issue_number": 0},
    {"repo": "org/repo", "issue_number": "2"},
    {"repo": "org/repo", "task_id": 3, "issue_number": 2},
])
def test_invalid_issue_identity(task_gateway, bad):
    assert task_request("POST", "/api/v1/tasks/spawn", bad)[0] == 400
    task_gateway[0].assert_not_called()


def test_failed_lifecycle_never_marks_done(task_gateway):
    _, github = task_gateway
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    def api(method, path, body=None):
        if path.endswith("/issues/2"):
            return {"number": 2, "html_url": "https://github.com/org/repo/issues/2"}
        if method == "GET" and "/labels" in path:
            return [{"name": "queued"}, {"name": "done"}]
        if method == "GET" and "/comments" in path:
            return [{"body": f"<!-- code-factory:{task_id} -->\nCode Factory: failed."}]
        return {}
    github.side_effect = api
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "review-rejected"}
    assert task_request("POST", f"/api/v1/tasks/{task_id}", result)[1]["lifecycle"] == "synced"
    assert any(call.args[0] == "DELETE" and call.args[1].endswith("/done") for call in github.call_args_list)
    assert any(call.args[0] == "POST" and call.args[2] == {"labels": ["failed"]} for call in github.call_args_list)
    assert not any(call.args[0] == "POST" and "/comments" in call.args[1] for call in github.call_args_list)

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
