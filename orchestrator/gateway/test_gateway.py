"""Tests for gateway task intake, credential validation, and lifecycle handling."""

import base64
import copy
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
import server
import yaml

from helm.test_registry import AUTH_CREDENTIALS


@pytest.fixture
def helm_values():
    return {"credentials": copy.deepcopy(AUTH_CREDENTIALS)}


@pytest.fixture
def task_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(server, "TOKEN", "test-token")
    monkeypatch.setattr(server, "SUPERVISOR_TOKEN", "supervisor-test-token")
    monkeypatch.setenv("GATEWAY_TRANSPORT_MODE", "local-http")
    monkeypatch.setattr(server, "_validate_repository", Mock())
    monkeypatch.setattr(server.secrets, "token_urlsafe", Mock(return_value="callback-test-only"))
    stored = {}
    def kubernetes(method, path, body=None):
        name = path.rsplit("/", 1)[1]
        if method == "POST":
            name = body["metadata"]["name"]
            if name in stored:
                raise server.KubernetesError(409)
            stored[name] = copy.deepcopy(body)
            stored[name]["metadata"]["uid"] = "credential-uid"
            return copy.deepcopy(stored[name])
        if name not in stored:
            raise server.KubernetesError(404)
        if method == "DELETE":
            if body["preconditions"]["uid"] != stored[name]["metadata"]["uid"]:
                raise server.KubernetesError(409)
            return stored.pop(name)
        return copy.deepcopy(stored[name])
    monkeypatch.setattr(server, "_k8s_request", Mock(side_effect=kubernetes))
    create = Mock(return_value={"name": "assigned-worker", "uid": "uid"})
    github = Mock(side_effect=RuntimeError("GitHub unavailable"))
    monkeypatch.setattr(server, "_github", github)
    return create, github


def task_request(method, path, payload=None, token="test-token", repository_token="repo-test-only", callback="callback-test-only", supervisor=True):
    if (supervisor and method == "POST" and path.startswith("/api/v1/tasks/") and isinstance(payload, dict)
        and payload.get("status") in ("completed", "failed") and callback == "callback-test-only"):
        path = path.replace("/api/v1/tasks/", "/api/v1/supervisor/tasks/", 1)
        token = "supervisor-test-token"
    body = json.dumps(payload).encode()
    handler = server.Handler.__new__(server.Handler)
    handler.command = method
    handler.headers = {"Authorization": f"token {token}", "Content-Length": str(len(body)),
                       "X-Repository-Token": repository_token, "X-Task-Token": callback}
    handler.client_address = ("127.0.0.1", 1234)
    handler.path = path
    handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
    handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
    getattr(handler, f"do_{method}")()
    return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())


def issue_payload(repo="org/repo"):
    return {"name": "drain", "ref": "refs/heads/main", "payload": {"repo": repo, "issue_number": 2}}


def test_task_list_is_bounded_filtered_and_sanitized(task_gateway):
    first = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    second = task_request("POST", "/api/v1/tasks/spawn", issue_payload("org/other"))[1]["task_id"]
    code, page = task_request("GET", "/api/v1/tasks?limit=1")
    assert code == 200 and page["total"] == 2
    assert page["tasks"][0]["task_id"] == second
    assert task_request("GET", "/api/v1/tasks?limit=1&offset=1")[1]["tasks"][0]["task_id"] == first
    assert task_request("GET", "/api/v1/tasks?repo=ORG/REPO&status=queued")[1]["total"] == 1
    assert task_request("GET", "/api/v1/tasks?status=completed")[1]["tasks"] == []
    assert not {"payload", "result", "credential_ref", "callback_hash", "job"} & page["tasks"][0].keys()
    assert "repo-test-only" not in json.dumps(page)
    assert task_request("GET", "/api/v1/tasks", token="wrong")[0] == 401


@pytest.mark.parametrize("query", ["limit=101", "offset=-1", "limit=no", "status=unknown", "repo=../bad",
                                     "limit=1&limit=2", "unknown=value"])
def test_task_list_rejects_invalid_queries(task_gateway, query):
    assert task_request("GET", "/api/v1/tasks?" + query)[0] == 400


def test_gateway_auth_has_no_global_github_fallback():
    output = subprocess.check_output([
        sys.executable, "-c", "import server; print(bool(server.TOKEN))",
    ], cwd=os.path.dirname(server.__file__),
        env={**os.environ, "GITHUB_TOKEN": "legacy-test-only", "CODEFACTORY_AUTH_TOKEN": ""}, text=True)
    assert output.strip() == "False"


def test_task_secret_is_immutable_and_duplicate_cannot_replace(task_gateway):
    _, github = task_gateway
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    db = server._task_db()
    task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    db.close()
    assert "repo-test-only" not in json.dumps(dict(task))
    assert "callback-test-only" not in json.dumps(dict(task))
    secret = server._k8s_request("GET", server._secret_path(task["credential_ref"]))
    assert secret["immutable"] is True
    assert task["credential_uid"] == secret["metadata"]["uid"]
    assert "ownerReferences" not in secret["metadata"]
    assert base64.b64decode(secret["data"]["repository-token"]) == b"repo-test-only"
    assert task_request("POST", "/api/v1/tasks/spawn", issue_payload(), repository_token="different-repo-test-only")[0] == 202
    assert server._k8s_request("GET", server._secret_path(task["credential_ref"])) == secret
    assert task_request("POST", f"/api/v1/tasks/{task_id}", {
        "task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "task-execution-failed",
    }, callback="wrong-callback")[0] == 403
    github.assert_not_called()


def test_acceptance_metrics_count_initial_and_retry_but_not_replay(task_gateway, monkeypatch):
    initial_before = server.ACCEPTED_TASKS.labels(kind="initial")._value.get()
    retry_before = server.ACCEPTED_TASKS.labels(kind="retry")._value.get()
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    task_request("POST", "/api/v1/tasks/spawn", issue_payload())
    finish_failed_task(task_gateway, monkeypatch, task_id)
    task_request("POST", f"/api/v1/tasks/{task_id}/retry", {"retry_key": "manual-1"})

    assert server.ACCEPTED_TASKS.labels(kind="initial")._value.get() == initial_before + 1
    assert server.ACCEPTED_TASKS.labels(kind="retry")._value.get() == retry_before + 1


def finish_failed_task(task_gateway, monkeypatch, task_id):
    monkeypatch.setattr(server, "_sync_lifecycle", Mock())
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2,
              "status": "failed", "error": "validation-failed"}
    code, state = task_request("POST", f"/api/v1/tasks/{task_id}", result)
    assert code == 200 and state["lifecycle"] == "synced"
    return task_request("GET", f"/api/v1/tasks/{task_id}")[1]


def test_retry_preserves_history_and_uses_new_credentials(task_gateway, monkeypatch):
    original = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    previous = finish_failed_task(task_gateway, monkeypatch, original)
    monkeypatch.setattr(server.secrets, "token_urlsafe", Mock(return_value="new-callback-test-only"))
    code, retried = task_request("POST", f"/api/v1/tasks/{original}/retry", {"retry_key": "manual-1"},
                                 repository_token="new-repo-test-only")
    assert code == 202 and retried["task_id"] != original
    task_id = retried["task_id"]
    state = task_request("GET", f"/api/v1/tasks/{task_id}")[1]
    assert state["retry_of"] == original and state["status"] == "queued"
    parent = task_request("GET", f"/api/v1/tasks/{original}")[1]
    assert parent.pop("retry_task_id") == task_id
    previous.pop("retry_task_id")
    assert parent == previous
    db = server._task_db()
    row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    secret = server._k8s_request("GET", server._secret_path(row["credential_ref"]))
    assert base64.b64decode(secret["data"]["repository-token"]) == b"new-repo-test-only"
    assert "new-repo-test-only" not in json.dumps(dict(row))
    db.close()
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "validation-failed"}
    assert task_request("POST", f"/api/v1/tasks/{task_id}", result, callback="wrong-callback")[0] == 403
    assert task_request("POST", f"/api/v1/tasks/{task_id}", result, callback="new-callback-test-only")[0] == 400
    assert task_request("POST", f"/api/v1/supervisor/tasks/{task_id}", result,
                        token="supervisor-test-token")[0] == 200


def test_retry_replays_without_creating_another_job(task_gateway, monkeypatch):
    original = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    finish_failed_task(task_gateway, monkeypatch, original)
    path = f"/api/v1/tasks/{original}/retry"
    first = task_request("POST", path, {"retry_key": "manual-1"})[1]
    code, replay = task_request("POST", path, {"retry_key": "manual-1"})
    assert code == 202 and replay["replayed"] and replay["task_id"] == first["task_id"]
    assert task_request("POST", path, {"retry_key": "manual-2"})[0] == 409
    assert task_gateway[0].call_count == 0
    assert task_request("POST", "/api/v1/tasks/spawn", issue_payload())[0] == 409
    child = first["task_id"]
    finish_failed_task(task_gateway, monkeypatch, child)
    code, grandchild = task_request("POST", f"/api/v1/tasks/{child}/retry", {"retry_key": "manual-2"})
    assert code == 202 and grandchild["task_id"] not in (original, child)


def test_retry_requires_terminal_synced_failure(task_gateway, monkeypatch):
    original = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    path = f"/api/v1/tasks/{original}/retry"
    assert task_request("POST", path, {"retry_key": "manual-1"})[1]["error"] == "retry-requires-failed-task"
    result = {"task_id": original, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "validation-failed"}
    task_request("POST", f"/api/v1/tasks/{original}", result)
    assert task_request("POST", path, {"retry_key": "manual-1"})[0] == 202
    task_gateway[0].assert_not_called()


def test_retry_can_recover_from_permanent_credential_failure(task_gateway, monkeypatch):
    original = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    monkeypatch.setattr(server, "_sync_lifecycle", Mock(side_effect=server.CredentialError("credential-expired")))
    result = {"task_id": original, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "repository-credential-expired"}
    assert task_request("POST", f"/api/v1/tasks/{original}", result)[1]["lifecycle"] == "synced"
    code, retry = task_request("POST", f"/api/v1/tasks/{original}/retry", {"retry_key": "new-credential"},
                              repository_token="new-repo-test-only")
    assert code == 202 and retry["task_id"] != original


@pytest.mark.parametrize("payload", [{}, {"retry_key": ""}, {"retry_key": 1}, {"retry_key": "invalid/path"},
                                    {"retry_key": "manual-1", "repo": "other/repo"}])
def test_retry_rejects_invalid_requests(task_gateway, payload):
    original = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    assert task_request("POST", f"/api/v1/tasks/{original}/retry", payload)[0] == 400
    task_gateway[0].assert_not_called()


def test_retry_revalidates_repository_access(task_gateway, monkeypatch):
    original = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    finish_failed_task(task_gateway, monkeypatch, original)
    path = f"/api/v1/tasks/{original}/retry"
    assert task_request("POST", path, {"retry_key": "manual-1"}, token="wrong")[0] == 401
    monkeypatch.setattr(server, "_validate_repository", Mock(side_effect=server.CredentialError("repository-credential-denied")))
    assert task_request("POST", path, {"retry_key": "manual-1"})[0] == 403
    task_gateway[0].assert_not_called()


def test_concurrent_retries_have_only_one_child(task_gateway, monkeypatch):
    original = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    finish_failed_task(task_gateway, monkeypatch, original)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda key: task_request("POST", f"/api/v1/tasks/{original}/retry", {"retry_key": key}),
                                  ["manual-1", "manual-2"]))
    assert sorted(code for code, _body in responses) == [202, 409]
    assert task_gateway[0].call_count == 0
    db = server._task_db()
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE retry_of=?", (original,)).fetchone()[0] == 1
    db.close()


def test_secret_cleanup_retry_does_not_repeat_github_updates(task_gateway, monkeypatch):
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    sync = Mock()
    monkeypatch.setattr(server, "_sync_lifecycle", sync)
    delete = Mock(side_effect=server.KubernetesError(503))
    monkeypatch.setattr(server, "_delete_task_secret", delete)
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "task-execution-failed"}
    path = f"/api/v1/tasks/{task_id}"
    assert task_request("POST", path, result)[1]["lifecycle"] == "cleanup-pending"
    delete.side_effect = None
    assert task_request("POST", path, result)[1]["lifecycle"] == "synced"
    assert sync.call_count == 0


def test_secret_creation_failure_is_durable_without_dispatch(task_gateway, monkeypatch):
    monkeypatch.setattr(server, "_k8s_request", Mock(side_effect=server.KubernetesError(503)))
    code, body = task_request("POST", "/api/v1/tasks/spawn", issue_payload())
    assert code == 503 and body["error"] == "credential-create-failed"
    task_gateway[0].assert_not_called()
    assert task_request("GET", f"/api/v1/tasks/{body['task_id']}")[1]["status"] == "failed"
    assert task_request("POST", "/api/v1/tasks/spawn", issue_payload())[0] == 409


def test_secret_creation_lost_response_retains_unverified_secret(task_gateway, monkeypatch):
    monkeypatch.setattr(server, "_sync_lifecycle", Mock())
    request = server._k8s_request
    def lost_creation_response(method, path, body=None):
        response = request(method, path, body)
        if method == "POST":
            raise server.KubernetesError(503)
        return response
    monkeypatch.setattr(server, "_k8s_request", lost_creation_response)
    code, body = task_request("POST", "/api/v1/tasks/spawn", issue_payload())
    assert code == 503
    task = task_request("GET", f"/api/v1/tasks/{body['task_id']}")[1]
    assert task["status"] == "failed"
    assert task["lifecycle"] == "credential-binding-mismatch" and task["credential_cleanup"] == "pending"
    assert request("GET", server._secret_path("task-credential-" + body["task_id"]))["immutable"] is True


def test_stale_secret_is_neither_adopted_nor_deleted(task_gateway):
    task_id = "issue-" + server.hashlib.sha256(b"org/repo#2").hexdigest()[:32]
    server._create_task_secret(task_id, "org/repo", "previous-repo-test-only", "previous-callback")
    assert task_request("POST", "/api/v1/tasks/spawn", issue_payload())[0] == 503
    task_gateway[0].assert_not_called()
    task_gateway[1].assert_not_called()
    task = task_request("GET", f"/api/v1/tasks/{task_id}")[1]
    assert task["lifecycle"] == "credential-binding-mismatch"
    assert task["credential_cleanup"] == "pending"
    secret = server._k8s_request("GET", server._secret_path("task-credential-" + task_id))
    assert base64.b64decode(secret["data"]["repository-token"]) == b"previous-repo-test-only"


def test_secret_cleanup_has_uid_precondition(task_gateway, monkeypatch):
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    db = server._task_db()
    task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    db.close()
    secret = server._k8s_request("GET", server._secret_path(task["credential_ref"]))
    secret["metadata"]["uid"] = "credential-uid"
    request = Mock(return_value=secret)
    monkeypatch.setattr(server, "_k8s_request", request)
    server._delete_task_secret(task)
    request.assert_called_once_with("DELETE", server._secret_path(task["credential_ref"]), {
        "apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": "credential-uid"},
    })


@pytest.mark.parametrize("status", [401, 403])
def test_lifecycle_revocation_is_explicit_and_cleans_owned_secret(task_gateway, status):
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    task_gateway[1].side_effect = server.urllib.error.HTTPError("https://api.github.com/repos/org/repo", status, "repo-test-only", {}, None)
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "repository-credential-denied"}
    assert task_request("POST", f"/api/v1/tasks/{task_id}", result)[1]["lifecycle"] == "synced"
    task = task_request("GET", f"/api/v1/tasks/{task_id}")[1]
    assert task["result"] == result and task["credential_cleanup"] == "deleted"


def test_sync_endpoint_recovers_after_restart_without_new_credential(task_gateway, monkeypatch):
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    path = f"/api/v1/tasks/{task_id}"
    assert task_request("POST", path + "/sync", {})[0] == 409
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "task-execution-failed"}
    assert task_request("POST", path, result)[1]["lifecycle"] == "synced"
    monkeypatch.setattr(server, "_sync_lifecycle", Mock())
    assert task_request("POST", path + "/sync", {}, callback="", repository_token="")[1]["lifecycle"] == "synced"
    assert task_request("GET", path)[1]["credential_cleanup"] == "deleted"
    assert task_request("POST", path + "/sync", {"token": "replacement"})[0] == 400


def test_secret_delete_lost_response_recovers(task_gateway, monkeypatch):
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    monkeypatch.setattr(server, "_sync_lifecycle", Mock())
    request = server._k8s_request
    def lost_response(method, path, body=None):
        response = request(method, path, body)
        if method == "DELETE":
            raise server.KubernetesError(503)
        return response
    monkeypatch.setattr(server, "_k8s_request", lost_response)
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "completed",
              "patch": "patch", "pr_url": "https://github.com/org/repo/pull/3"}
    path = f"/api/v1/tasks/{task_id}"
    assert task_request("POST", path, result)[1]["lifecycle"] == "cleanup-pending"
    assert task_request("POST", path, result)[1]["lifecycle"] == "synced"


@pytest.mark.parametrize("problem,expected", [(404, "synced"), (409, "credential-binding-mismatch"),
                                            (503, "cleanup-pending")])
def test_secret_api_missing_is_not_transient_failure(task_gateway, monkeypatch, problem, expected):
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    monkeypatch.setattr(server, "_k8s_request", Mock(side_effect=server.KubernetesError(problem)))
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "task-execution-failed"}
    assert task_request("POST", f"/api/v1/tasks/{task_id}", result)[1]["lifecycle"] == expected
    assert task_request("GET", f"/api/v1/tasks/{task_id}")[1]["result"] == result


def test_secret_cleanup_without_persisted_uid_never_calls_kubernetes(task_gateway, monkeypatch):
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    db = server._task_db()
    db.execute("UPDATE tasks SET credential_uid=NULL WHERE task_id=?", (task_id,))
    db.close()
    request = Mock(side_effect=AssertionError("Unverified Secret must not be read or deleted"))
    monkeypatch.setattr(server, "_k8s_request", request)
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2,
              "status": "failed", "error": "task-execution-failed"}
    assert task_request("POST", f"/api/v1/tasks/{task_id}", result)[1]["lifecycle"] == "credential-binding-mismatch"
    assert task_request("GET", f"/api/v1/tasks/{task_id}")[1]["credential_cleanup"] == "pending"
    request.assert_not_called()


def test_completed_result_is_durable_during_secret_api_outage(task_gateway, monkeypatch):
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    monkeypatch.setattr(server, "_k8s_request", Mock(side_effect=server.KubernetesError(503)))
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "completed",
              "patch": "valid patch", "pr_url": "https://github.com/org/repo/pull/3"}
    assert task_request("POST", f"/api/v1/tasks/{task_id}", result)[1]["lifecycle"] == "cleanup-pending"
    assert task_request("GET", f"/api/v1/tasks/{task_id}")[1]["result"] == result


def test_token_shaped_result_is_rejected_without_secret_api(task_gateway, monkeypatch):
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    monkeypatch.setattr(server, "_k8s_request", Mock(side_effect=server.KubernetesError(503)))
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "completed",
              "patch": "github_pat_" + "synthetic" * 10, "pr_url": "https://github.com/org/repo/pull/3"}
    assert task_request("POST", f"/api/v1/tasks/{task_id}", result)[0] == 400
    assert task_request("GET", f"/api/v1/tasks/{task_id}")[1]["result"] is None


def test_expired_secret_preserves_terminal_result(task_gateway, monkeypatch):
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    now = server.time.time()
    monkeypatch.setattr(server.time, "time", lambda: now + 86401)
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "repository-credential-expired"}
    assert task_request("POST", f"/api/v1/tasks/{task_id}", result)[1]["lifecycle"] == "synced"
    assert task_request("GET", f"/api/v1/tasks/{task_id}")[1]["result"] == result


@pytest.mark.parametrize("payload", [{"repository_token": "untrusted"}, {"payload": {"repo": "org/repo", "issue_number": 2, "description": "repo-test-only"}}])
def test_credentials_rejected_in_payload(task_gateway, payload):
    assert task_request("POST", "/api/v1/tasks/spawn", payload)[0] == 400
    task_gateway[0].assert_not_called()


def test_intake_requires_explicit_transport_mode(task_gateway, monkeypatch):
    monkeypatch.delenv("GATEWAY_TRANSPORT_MODE")
    assert task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1] == {"error": "transport-not-allowed"}
    task_gateway[0].assert_not_called()


def test_edge_tls_intake_keeps_authentication_without_proxy_headers(task_gateway, monkeypatch):
    monkeypatch.setenv("GATEWAY_TRANSPORT_MODE", "edge-tls")
    assert task_request("POST", "/api/v1/tasks/spawn", issue_payload(), token="invalid")[0] == 401
    task_gateway[0].assert_not_called()
    code, result = task_request("POST", "/api/v1/tasks/spawn", issue_payload())
    assert code == 202 and result["status"] == "queued"
    task_gateway[0].assert_not_called()


def test_intake_commits_queued_task_without_creating_job(task_gateway):
    create_job, _github = task_gateway
    code, accepted = task_request("POST", "/api/v1/tasks/spawn", issue_payload())
    assert code == 202 and accepted["status"] == "queued"
    create_job.assert_not_called()
    task_id = accepted["task_id"]
    db = server._task_db()
    try:
        row = db.execute("SELECT status, config_version, config_hash FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        db.close()
    assert row["status"] == "queued"
    assert row["config_version"] == "1" and len(row["config_hash"]) == 64
    assert task_request("POST", "/api/v1/tasks/spawn", issue_payload())[0] == 202
    create_job.assert_not_called()


def test_worker_candidate_is_durable_but_not_terminal(task_gateway):
    create_job, _github = task_gateway
    task_id = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    candidate = {"task_id": task_id, "repo": "org/repo", "issue_number": 2,
                 "status": "candidate", "role": "coder", "patch": "verified later",
                 "validation": {"status": "passed"}}
    code, report = task_request("POST", f"/api/v1/tasks/{task_id}", candidate)
    assert code == 200 and report["status"] == "candidate"
    state = task_request("GET", f"/api/v1/tasks/{task_id}")[1]
    assert state["status"] == "queued" and state["result"] == candidate
    assert task_request("POST", f"/api/v1/tasks/{task_id}", {
        **candidate, "status": "completed", "pr_url": "https://github.com/org/repo/pull/3",
    }, supervisor=False)[0] == 400
    terminal = {"task_id": task_id, "status": "completed",
                "pr_url": "https://github.com/org/repo/pull/3"}
    assert task_request("POST", f"/api/v1/supervisor/tasks/{task_id}", terminal, token="wrong")[0] == 401
    create_job.assert_not_called()


@pytest.mark.parametrize("mode,address", [("local-http", "127.0.0.1"), ("edge-tls", "0.0.0.0")])
def test_transport_server_binding(monkeypatch, mode, address):
    monkeypatch.setenv("GATEWAY_TRANSPORT_MODE", mode)
    factory = Mock()
    monkeypatch.setattr(server, "ThreadingHTTPServer", factory)
    assert server._create_server() == factory.return_value
    factory.assert_called_once_with((address, 8000), server.Handler)


@pytest.mark.parametrize("mode", ["", "unknown"])
def test_transport_server_rejects_unknown_mode(monkeypatch, mode):
    monkeypatch.setenv("GATEWAY_TRANSPORT_MODE", mode)
    factory = Mock()
    monkeypatch.setattr(server, "ThreadingHTTPServer", factory)
    with pytest.raises(ValueError, match="gateway-transport-mode-required"):
        server._create_server()
    factory.assert_not_called()


@pytest.mark.parametrize("mode,address,allowed", [
    ("edge-tls", "192.0.2.6", True),
    ("local-http", "127.0.0.1", True),
    ("local-http", "::1", True),
    ("local-http", "192.0.2.6", False),
    ("unknown", "127.0.0.1", False),
    ("", "127.0.0.1", False),
])
def test_transport_mode_does_not_trust_forwarded_headers(monkeypatch, mode, address, allowed):
    monkeypatch.setenv("GATEWAY_TRANSPORT_MODE", mode)
    handler = server.Handler.__new__(server.Handler)
    handler.headers = {"X-Forwarded-Proto": "https"}
    handler.client_address = (address, 1234)
    assert handler._transport_allowed() is allowed
    handler.headers = {}
    assert handler._transport_allowed() is allowed


@pytest.mark.parametrize("status,reason", [(401, "denied"), (403, "denied"), (404, "denied"), (503, "unavailable")])
def test_repository_validation_errors_are_sanitized(monkeypatch, status, reason):
    error = server.urllib.error.HTTPError("https://api.github.com/repos/org/repo", status, "repo-test-only", {}, io.BytesIO(b"repo-test-only"))
    monkeypatch.setattr(server, "_github", Mock(side_effect=error))
    with pytest.raises(server.CredentialError, match=reason) as raised:
        server._validate_repository("org/repo", 2, "repo-test-only")
    assert "repo-test-only" not in str(raised.value)


def test_repository_validation_probes_with_only_originating_credential(monkeypatch):
    github = Mock(side_effect=[{"full_name": "ORG/Repo", "permissions": {"pull": True, "push": False}}, [], [], [],
                               {"number": 2, "html_url": "https://github.com/org/repo/issues/2"}])
    monkeypatch.setattr(server, "_github", github)
    server._validate_repository("org/repo", 2, "repo-test-only")
    assert len(github.call_args_list) == 5
    assert all(call.args[0] == "GET" and call.kwargs == {"token": "repo-test-only"} for call in github.call_args_list)


def test_kubernetes_errors_never_include_secret_response(monkeypatch):
    monkeypatch.setattr(server, "_k8s_bearer_token", lambda: "sa-test-only")
    monkeypatch.setattr(server, "_k8s_ssl_context", lambda: None)
    monkeypatch.setattr(server, "_k8s_base_url", lambda: "https://kubernetes.test")
    error = server.urllib.error.HTTPError("https://kubernetes.test/api", 503, "repo-test-only", {}, io.BytesIO(b"repo-test-only"))
    opener = Mock()
    opener.open.side_effect = error
    monkeypatch.setattr(server.urllib.request, "build_opener", Mock(return_value=opener))
    log = Mock()
    monkeypatch.setattr(server, "logger", log)
    with pytest.raises(server.KubernetesError) as raised:
        server._k8s_request("POST", server._secret_path(), {"data": {"repository-token": "repo-test-only"}})
    assert raised.value.status == 503
    assert "repo-test-only" not in str(raised.value) + repr(log.mock_calls)


def test_task_callback_cannot_report_another_repository_task(task_gateway, monkeypatch):
    monkeypatch.setattr(server.secrets, "token_urlsafe", Mock(side_effect=["callback-first", "callback-second"]))
    first = task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["task_id"]
    second = task_request("POST", "/api/v1/tasks/spawn", issue_payload("org/other"))[1]["task_id"]
    result = {"task_id": second, "repo": "org/other", "issue_number": 2, "status": "failed", "error": "task-execution-failed"}
    assert first != second
    assert task_request("POST", f"/api/v1/tasks/{second}", result, callback="callback-first")[0] == 403
    assert task_request("GET", f"/api/v1/tasks/{second}")[1]["status"] == "queued"


@pytest.mark.parametrize("url,accepted", [
    ("https://intake.test", True),
    ("https://intake.test/", True),
    ("https://intake.test:8443/", True),
    ("http://intake.test", False),
    ("https://user:pass@intake.test", False),
    ("https://intake.test/api/v1/tasks/spawn", False),
    ("https://intake.test?key=value", False),
    ("https://intake.test#fragment", False),
    ("https:///", False),
    ("https://intake.test\n", False),
    ("https://intake.test\\other", False),
])
def test_drain_workflow_credential_headers_and_https(monkeypatch, tmp_path, capsys, url, accepted):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issues")
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/drain.yaml").read_text())
    step = next(step for step in workflow["jobs"]["send-to-orchestrator"]["steps"] if step.get("shell") == "python")
    assert step["env"]["CODEFACTORY_URL"] == "${{ secrets.CODEFACTORY_URL }}"
    assert step["env"]["CODEFACTORY_AUTH_TOKEN"] == "${{ secrets.CODEFACTORY_AUTH_TOKEN }}"
    assert step["env"]["REPO_GITHUB_PAT"] == "${{ secrets.REPO_GITHUB_PAT }}"
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"repository": {"full_name": "org/repo"}, "issue": {"number": 2, "title": "untrusted $()"}}))
    for key, value in {"CODEFACTORY_URL": url, "CODEFACTORY_AUTH_TOKEN": "gateway-test-only", "REPO_GITHUB_PAT": "repo-test-only",
                       "GITHUB_TOKEN": "legacy-test-only", "REGISTRY_TOKEN": "registry-test-only", "GITHUB_EVENT_PATH": str(event),
                       "GITHUB_REPOSITORY": "org/repo"}.items():
        monkeypatch.setenv(key, value)
    response = Mock(status=202)
    response.read.return_value = json.dumps({"task_id": "issue-" + "a" * 32, "status": "running"}).encode()
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = context
    build = Mock(return_value=opener)
    monkeypatch.setattr(server.urllib.request, "build_opener", build)
    script_file = tmp_path / "intake.py"
    script_file.write_text(step["run"])
    if not accepted:
        with pytest.raises(SystemExit):
            runpy.run_path(str(script_file))
        build.assert_not_called()
        return
    namespace = runpy.run_path(str(script_file))
    request = opener.open.call_args.args[0]
    assert request.full_url == url.rstrip("/") + "/api/v1/tasks/spawn"
    assert request.get_header("Authorization") == "token gateway-test-only"
    assert request.get_header("X-repository-token") == "repo-test-only"
    assert json.loads(request.data) == {"repo": "org/repo", "issue_number": 2}
    with pytest.raises(ValueError, match="redirect rejected"):
        namespace["NoRedirect"]().redirect_request(None, None, 307, "", {}, "https://different.test")
    output = capsys.readouterr().out
    assert all(value not in output + request.data.decode() for value in ("gateway-test-only", "repo-test-only", "registry-test-only", "legacy-test-only"))


@pytest.mark.parametrize("status", [401, 403, 404, 409, 503])
def test_drain_workflow_http_errors_report_only_status(monkeypatch, tmp_path, capsys, status):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issues")
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/drain.yaml").read_text())
    step = next(step for step in workflow["jobs"]["send-to-orchestrator"]["steps"] if step.get("shell") == "python")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"repository": {"full_name": "org/repo"}, "issue": {"number": 2}}))
    for key, value in {
        "CODEFACTORY_URL": "https://intake.test",
        "CODEFACTORY_AUTH_TOKEN": "gateway-test-only",
        "REPO_GITHUB_PAT": "repo-test-only",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": "org/repo",
    }.items():
        monkeypatch.setenv(key, value)
    body = io.BytesIO(b'{"error":"gateway-test-only repo-test-only"}')
    error = server.urllib.error.HTTPError(
        "https://intake.test", status, "gateway-test-only", {"X-Secret": "repo-test-only"}, body,
    )
    opener = Mock()
    opener.open.side_effect = error
    monkeypatch.setattr(server.urllib.request, "build_opener", Mock(return_value=opener))
    script_file = tmp_path / "intake.py"
    script_file.write_text(step["run"])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(script_file))
    assert str(raised.value) == f"Task intake failed: HTTP {status}"
    output = capsys.readouterr()
    assert output.out == output.err == ""
    assert body.tell() == 0


@pytest.mark.parametrize("task_id,valid", [("issue-" + "a" * 32, True), ("../../another/path", False), ("", False)])
def test_drain_manual_retry_uses_stable_run_key(monkeypatch, tmp_path, task_id, valid):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/drain.yaml").read_text())
    step = next(step for step in workflow["jobs"]["send-to-orchestrator"]["steps"] if step.get("shell") == "python")
    assert step["env"]["RETRY_TASK_ID"] == "${{ inputs.retry_task_id }}"
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"repository": {"full_name": "org/repo"}}))
    for key, value in {
        "CODEFACTORY_URL": "https://intake.test",
        "CODEFACTORY_AUTH_TOKEN": "gateway-test-only",
        "REPO_GITHUB_PAT": "repo-test-only",
        "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "org/repo",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_RUN_ID": "12345", "RETRY_TASK_ID": task_id,
    }.items():
        monkeypatch.setenv(key, value)
    response = Mock(status=202)
    response.read.return_value = json.dumps({"task_id": "issue-" + "b" * 32, "status": "running"}).encode()
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = context
    monkeypatch.setattr(server.urllib.request, "build_opener", Mock(return_value=opener))
    script_file = tmp_path / "retry.py"
    script_file.write_text(step["run"])
    if not valid:
        with pytest.raises(SystemExit):
            runpy.run_path(str(script_file))
        opener.open.assert_not_called()
        return
    runpy.run_path(str(script_file))
    request = opener.open.call_args.args[0]
    assert request.full_url == f"https://intake.test/api/v1/tasks/{task_id}/retry"
    assert json.loads(request.data) == {"retry_key": "12345"}
    assert request.get_header("Authorization") == "token gateway-test-only"
    assert request.get_header("X-repository-token") == "repo-test-only"


def test_helm_credential_references_and_rbac(helm_values):
    if not shutil.which("helm"):
        pytest.skip("helm unavailable")
    root = Path(__file__).resolve().parents[2]
    render = subprocess.run(["helm", "template", "credential-check", str(root / "helm/orchestrator"), "--values", "-"],
                            input=yaml.safe_dump(helm_values), capture_output=True, text=True, check=True)
    documents = [document for document in yaml.safe_load_all(render.stdout) if document]
    role = next(document for document in documents if document["kind"] == "Role"
                and document["metadata"]["name"] == "orchestrator-gateway")
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create", "delete"]},
    ]
    job = next(document for document in documents if document["kind"] == "Job")
    assert job["spec"]["suspend"] is True
    for document in documents:
        if document["kind"] not in ("Deployment", "Job"):
            continue
        pod = document["spec"]["template"]["spec"]
        assert pod["imagePullSecrets"] == [{"name": "ghcr-read-packages-pull-secret"}]
        for container in pod["containers"]:
            env = {entry["name"]: entry for entry in container.get("env", [])}
            assert not {"GITHUB_TOKEN", "REPO_GITHUB_PAT"} & env.keys()
            if container["name"] == "gateway":
                assert env["CODEFACTORY_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"] == {
                    "name": "orchestrator-gateway-auth", "key": "gateway-auth-token",
                }
            else:
                assert "CODEFACTORY_AUTH_TOKEN" not in env
                if container["name"] == "coder":
                    assert "JUDGE_URL" not in env and "JUDGE_MODEL" not in env
                if document["metadata"]["name"] == "orchestrator-supervisor":
                    assert pod["automountServiceAccountToken"] is True
    ingress = next(document for document in documents if document["kind"] == "Ingress")
    assert ingress["spec"]["tls"] and ingress["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.entrypoints"] == "websecure"
    secrets = [document for document in documents if document["kind"] == "Secret"]
    assert next(document for document in secrets if document["metadata"]["name"] == "orchestrator-gateway-auth")["data"].keys() == {"gateway-auth-token"}
    supervisor = next(document for document in documents if document["kind"] == "Deployment"
                      and document["metadata"]["name"] == "orchestrator-supervisor")
    supervisor_env = {entry["name"]: entry for entry in supervisor["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert supervisor_env["PR_GITHUB_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "orchestrator-github-pr-auth"
    assert supervisor_env["SUPERVISOR_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "orchestrator-supervisor-service-auth"


def test_helm_pins_nonsecret_catalog_runtime_and_p40_routes(helm_values):
    if not shutil.which("helm"):
        pytest.skip("helm unavailable")
    chart = Path(__file__).resolve().parents[2] / "helm/orchestrator"
    rendered = subprocess.run(["helm", "template", "runtime-check", str(chart), "--values", "-"],
                              input=yaml.safe_dump(helm_values), capture_output=True, text=True, check=True)
    documents = [document for document in yaml.safe_load_all(rendered.stdout) if document]
    catalog = next(document for document in documents if document["kind"] == "ConfigMap"
                   and document["metadata"]["name"].startswith("orchestrator-agent-catalog-"))
    data = catalog["data"]
    digest = server.hashlib.sha256((data["agent-roles.yaml"] + data["agent-roles.schema.json"]
                                    + data["runtime-config.json"]).encode()).hexdigest()
    assert catalog["metadata"]["annotations"]["code-factory/config-hash"] == digest
    assert catalog["metadata"]["name"].endswith(digest[:12])
    assert not any(secret_name in data["runtime-config.json"] for secret_name in
                   ("openai-api-key", "pr-github-token", "coder-api-key", "supervisor-auth-token"))
    runtime = json.loads(data["runtime-config.json"])
    assert runtime["coder_url"] == "http://llama-server-args-gpu0.inference.svc.cluster.local:8000/v1"
    assert runtime["judge_url"] == "http://llama-server-args-gpu1.inference.svc.cluster.local:8000/v1"
    worker = next(document for document in documents if document["kind"] == "Job")
    worker_env = {entry["name"]: entry for entry in worker["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert worker_env["CODER_URL"]["value"] == runtime["coder_url"]
    assert "JUDGE_URL" not in worker_env and "OPENAI_API_KEY" not in worker_env


@pytest.mark.parametrize("mode,secret,profile", [("internal", "gateway-test-tls", False), ("external", "", False),
                                               ("external", None, False), ("external", "", True)])
def test_helm_ingress_tls_termination(mode, secret, profile, helm_values):
    if not shutil.which("helm"):
        pytest.skip("helm unavailable")
    chart = Path(__file__).resolve().parents[2] / "helm/orchestrator"
    origin = "https://gateway.example.com"
    values = {"gateway": {"ingress": {"host": "orchestrator.local", "tlsTermination": mode, "tlsSecretName": secret}},
              "supervisor": {"config": {"acceptor-url": origin}}}
    if profile:
        values = yaml.safe_load((chart / "example.values.yaml").read_text())
        origin = values["supervisor"]["config"]["acceptor-url"]
    values.update(helm_values)
    rendered = subprocess.run(["helm", "template", "ingress-check", str(chart), "--values", "-"],
                              input=yaml.safe_dump(values), capture_output=True, text=True, check=True)
    documents = [document for document in yaml.safe_load_all(rendered.stdout) if document]
    ingress = next(document for document in documents if document["kind"] == "Ingress")
    assert ingress["spec"]["rules"][0]["host"] == "orchestrator.local"
    annotations = ingress["metadata"]["annotations"]
    if mode == "internal":
        assert annotations["traefik.ingress.kubernetes.io/router.entrypoints"] == "websecure"
        assert annotations["traefik.ingress.kubernetes.io/router.tls"] == "true"
        assert ingress["spec"]["tls"] == [{"hosts": ["orchestrator.local"], "secretName": secret}]
    else:
        assert annotations["traefik.ingress.kubernetes.io/router.entrypoints"] == "web"
        assert "traefik.ingress.kubernetes.io/router.tls" not in annotations
        assert "tls" not in ingress["spec"]
    job = next(document for document in documents if document["kind"] == "Job")
    env = {entry["name"]: entry for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    reference = env["ACCEPTOR_URL"]["valueFrom"]["configMapKeyRef"]
    config = next(document for document in documents if document["kind"] == "ConfigMap"
                  and document["metadata"]["name"] == reference["name"])
    assert config["data"][reference["key"]] == origin
    gateway = next(document for document in documents if document["kind"] == "Deployment"
                   and document["metadata"]["name"] == "orchestrator-gateway")
    env = {entry["name"]: entry.get("value") for entry in gateway["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["GATEWAY_TRANSPORT_MODE"] == "edge-tls"
    assert not {"TRUST_TLS_PROXY", "TRUSTED_TLS_PROXY_CIDRS"} & env.keys()


@pytest.mark.parametrize("setting", ["supervisor.config.acceptor-url=http://gateway", "gateway.transportMode=invalid",
                                    "gateway.transportMode=local-http",
                                    "gateway.ingress.tlsTermination=invalid", "gateway.ingress.tlsSecretName=",
                                    "gateway.ingress.tlsTermination=null", "gateway.ingress.tlsSecretName=null",
                                    "gateway.ingress.externalTls=true",
                                    "gateway.taskCredentialTtlSeconds=0", "gateway.auth.existingSecret=", "gateway.auth.token=conflicting-test-only"])
def test_helm_rejects_unsafe_credential_configuration(setting, helm_values):
    if not shutil.which("helm"):
        pytest.skip("helm unavailable")
    chart = Path(__file__).resolve().parents[2] / "helm/orchestrator"
    rendered = subprocess.run(["helm", "template", "credential-check", str(chart), "--values", "-", "--set", setting],
                              input=yaml.safe_dump(helm_values), capture_output=True, text=True, check=False)
    assert rendered.returncode != 0
    rejected_field = "/gateway/auth" if setting.startswith("gateway.auth.") else setting.split("=", 1)[0].split(".")[-1]
    assert rejected_field in rendered.stderr


@pytest.mark.parametrize("ttl", [None, 3600, 1, 604800])
def test_helm_task_job_ttl_reaches_submitted_job(monkeypatch, ttl, helm_values):
    if not shutil.which("helm"):
        pytest.skip("helm unavailable")
    chart = Path(__file__).resolve().parents[2] / "helm/orchestrator"
    values = helm_values
    if ttl is not None:
        values["gateway"] = {"taskJobTtlSecondsAfterFinished": ttl}
    rendered = subprocess.run(["helm", "template", "ttl-check", str(chart), "--values", "-"],
                              input=yaml.safe_dump(values), capture_output=True, text=True, check=True)
    documents = [document for document in yaml.safe_load_all(rendered.stdout) if document]
    seed = next(document for document in documents if document["kind"] == "Job")
    original = copy.deepcopy(seed)
    assert seed["spec"]["suspend"] is True
    assert seed["spec"]["backoffLimit"] == 0
    assert "ttlSecondsAfterFinished" not in seed["spec"]
    supervisor = next(document for document in documents if document["kind"] == "Deployment"
                      and document["metadata"]["name"] == "orchestrator-supervisor")
    env = {entry["name"]: entry.get("value")
           for entry in supervisor["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["TASK_JOB_TTL_SECONDS_AFTER_FINISHED"] == str(ttl if ttl is not None else 86400)
    gateway = next(document for document in documents if document["kind"] == "Deployment"
                   and document["metadata"]["name"] == "orchestrator-gateway")
    gateway_env = {entry["name"]: entry.get("value")
                   for entry in gateway["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "TASK_JOB_TTL_SECONDS_AFTER_FINISHED" not in gateway_env
    assert seed == original


@pytest.mark.parametrize("ttl", [0, -1, 604801, 1.5, "3600", True, None])
def test_helm_rejects_invalid_task_job_ttl(ttl, helm_values):
    if not shutil.which("helm"):
        pytest.skip("helm unavailable")
    chart = Path(__file__).resolve().parents[2] / "helm/orchestrator"
    helm_values["gateway"] = {"taskJobTtlSecondsAfterFinished": ttl}
    rendered = subprocess.run(["helm", "template", "ttl-check", str(chart), "--values", "-"],
                              input=yaml.safe_dump(helm_values),
                              capture_output=True, text=True, check=False)
    assert rendered.returncode != 0
    assert "taskJobTtlSecondsAfterFinished" in rendered.stderr


def test_gateway_github_transport_uses_only_explicit_repository_token(monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "gateway-test-only")
    response = Mock()
    response.read.return_value = b"{}"
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = context
    monkeypatch.setattr(server.urllib.request, "build_opener", Mock(return_value=opener))
    assert server._github("GET", "/repos/org/repo", token="repo-test-only") == {}
    request = opener.open.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer repo-test-only"
    assert request.full_url == "https://api.github.com/repos/org/repo"
    with pytest.raises(ValueError, match="redirect-rejected"):
        server.NoRedirect().redirect_request(None, None, 307, "", {}, "https://other.test")


@pytest.mark.parametrize("metadata,error", [
    ({"full_name": "org/other", "permissions": {"push": True}}, "identity-mismatch"),
    ({"full_name": "org/repo"}, "read-access-unverified"),
])
def test_repository_validation_rejects_wrong_identity_or_missing_read_access(monkeypatch, metadata, error):
    github = Mock(return_value=metadata)
    monkeypatch.setattr(server, "_github", github)
    with pytest.raises(server.CredentialError, match=error):
        server._validate_repository("org/repo", 2, "repo-test-only")
    assert github.call_args.kwargs == {"token": "repo-test-only"}


def test_repository_validation_accepts_repository_write_access(monkeypatch):
    github = Mock(side_effect=[
        {"full_name": "org/repo", "permissions": {"pull": True, "push": True}},
        [], [], [], {"number": 2, "html_url": "https://github.com/org/repo/issues/2"},
    ])
    monkeypatch.setattr(server, "_github", github)
    server._validate_repository("org/repo", 2, "repo-test-only")
    assert len(github.call_args_list) == 5
    assert all(call.kwargs == {"token": "repo-test-only"} for call in github.call_args_list)


def test_durable_repo_scoped_tasks_and_report_retry(task_gateway):
    create, github = task_gateway
    code, spawned = task_request("POST", "/api/v1/tasks/spawn", issue_payload())
    assert code == 202
    task_id = spawned["task_id"]
    assert task_id.startswith("issue-")
    assert task_request("GET", f"/api/v1/tasks/{task_id}")[1]["status"] == "queued"
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "completed",
              "patch": "real patch", "pr_url": "https://github.com/org/repo/pull/3"}
    path = f"/api/v1/tasks/{task_id}"
    code, report = task_request("POST", path, result)
    assert code == 200 and report["status"] == "completed"
    assert task_request("GET", path)[1]["result"] == result
    assert task_request("POST", path, result)[0] == 200
    github.assert_not_called()
    assert task_request("GET", "/api/v1/tasks/2?repo=org%2Frepo")[1]["task_id"] == task_id
    assert task_request("GET", "/api/v1/tasks/2")[0] == 404
    assert task_request("POST", "/api/v1/tasks/spawn", issue_payload())[1]["replayed"]
    assert create.call_count == 0
    assert task_request("POST", path, {**result, "status": "failed"})[0] == 409
    other = task_request("POST", "/api/v1/tasks/spawn", issue_payload("org/other"))[1]
    assert other["task_id"] != task_id
    assert task_request("POST", path, {**result, "repo": "org/other"})[0] == 409
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
    assert create.call_count == 0
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "validation-failed"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(lambda _: task_request("POST", f"/api/v1/tasks/{task_id}", result), range(4)))
    assert all(code == 200 for code, _ in reports)
    assert sync.call_count == 0


def test_gateway_does_not_dispatch_even_when_job_creator_fails(task_gateway):
    create, _ = task_gateway
    create.side_effect = RuntimeError("cluster unavailable")
    code, body = task_request("POST", "/api/v1/tasks/spawn", issue_payload())
    assert code == 202
    task = task_request("GET", f"/api/v1/tasks/{body['task_id']}")[1]
    assert task["status"] == "queued"
    assert task_request("POST", "/api/v1/tasks/spawn", issue_payload())[0] == 202
    assert create.call_count == 0


def test_task_storage_survives_new_process(task_gateway, tmp_path):
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
    assert task_request("GET", f"/api/v1/tasks/{task_id}")[1]["status"] == "failed"


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
    result = {"task_id": task_id, "repo": "org/repo", "issue_number": 2, "status": "failed", "error": "review-rejected"}
    assert task_request("POST", f"/api/v1/tasks/{task_id}", result)[1]["lifecycle"] == "synced"
    github.assert_not_called()

@pytest.mark.parametrize("legacy_value", ["", "*", "another/repo"])
def test_intake_relies_on_repository_credentials_not_allowlist(task_gateway, monkeypatch, legacy_value):
    monkeypatch.setenv("ALLOWED_REPOS", legacy_value)
    monkeypatch.setenv("ALLOWED_REPO", legacy_value)
    code, result = task_request("POST", "/api/v1/tasks/spawn", {"repo": "new/project", "issue_number": 2})
    assert code == 202
    assert result["repo"] == "new/project"
    server._validate_repository.assert_called_once_with("new/project", 2, "repo-test-only")


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


def test_gateway_has_no_kubernetes_job_dispatch_implementation():
    assert not hasattr(server, "_create_worker_job")
    assert not hasattr(server, "_clone_job_from_template")


@pytest.mark.parametrize("payload", [
    [], None, {"payload": []}, {"payload": None},
    {"name": "orchestrator-agent", "ref": "main", "payload": {"issue_number": None}},
    {"name": "orchestrator-agent", "ref": "main", "payload": {"issue_number": True}},
    {"name": "orchestrator-agent", "ref": "main", "payload": {"issue_number": "unknown"}},
    {"name": "orchestrator-agent", "ref": "main", "payload": {"issue_number": "../2"}},
    {"name": "orchestrator-agent", "ref": "main", "task_id": "", "payload": {"issue_number": 2}},
])
def test_gateway_rejects_malformed_assignment_before_job_creation(monkeypatch, payload):
    k8s_request = Mock(side_effect=AssertionError("No cluster calls expected"))
    monkeypatch.setattr(server, "_k8s_request", k8s_request)
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
    k8s_request.assert_not_called()
