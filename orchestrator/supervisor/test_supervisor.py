"""
Unit tests for the supervisor loop helpers (supervisor.py).

These exercise the pure helper functions and the pick-best / retry decision
logic, which are deterministic and do not require a running inference server.
"""


import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import supervisor as supervisor_module
from supervisor import (
    build_evaluation_prompt,
    claim_queued_task,
    make_eval_result,
    make_worker_output,
    node_execute_workers,
    passes_acceptance,
    should_retry,
    task_database,
)


def test_app_publication_refreshes_scoped_token_and_preserves_runtime(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_AUTH_TOKEN", "supervisor-test-only")
    now = [1000]
    monkeypatch.setattr(supervisor_module._candidate_workflow.time, "time", lambda: now[0])
    requests = []
    publications = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            assert limit == 8193
            return json.dumps({"repo": "org/repo", "token": "renewal-" + str(len(requests)),
                               "expires_at": now[0] + 3600}).encode()

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert request.full_url == "https://pinned.test/api/v1/supervisor/tasks/task/credentials"
            assert request.get_header("Authorization") == "token supervisor-test-only"
            assert timeout == 30
            return Response()

    def github(method, repo, path, body, *, token):
        publications.append(token)
        return {"ok": True}

    client = supervisor_module._candidate_workflow.AppGitHub(
        {"repo": "org/repo", "task_id": "task"}, {"acceptor_url": "https://pinned.test"}, github, Opener())
    assert client("POST", "org/repo", "/pulls") == {"ok": True}
    client("GET", "org/repo", "/pulls")
    assert len(requests) == 1
    now[0] += 3550
    client("POST", "org/repo", "/pulls")
    assert publications == ["renewal-1", "renewal-1", "renewal-2"]
    with pytest.raises(RuntimeError, match="^repository-mismatch$"):
        client("POST", "other/repo", "/pulls")
    assert len(requests) == 2


def test_app_publication_broker_failure_is_redacted(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_AUTH_TOKEN", "supervisor-test-only")

    class Opener:
        def open(self, *_args, **_kwargs):
            raise OSError("sensitive-response-token")

    client = supervisor_module._candidate_workflow.AppGitHub(
        {"repo": "org/repo", "task_id": "task"}, {"acceptor_url": "https://pinned.test"}, None, Opener())
    with pytest.raises(RuntimeError, match="^github-app-broker-unavailable$"):
        client("POST", "org/repo", "/pulls")


def test_supervisor_never_uses_legacy_global_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-test-only")
    monkeypatch.delenv("SUPERVISOR_MOCK_MODE", raising=False)
    with pytest.raises(RuntimeError, match="direct-spawn-disabled"):
        asyncio.run(node_execute_workers({"task_id": "task"}))


def test_supervisor_mock_is_explicit(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_MOCK_MODE", "true")
    state = {"task_id": "task", "worker_outputs": []}
    assert asyncio.run(node_execute_workers(state))["worker_outputs"]


def test_supervisor_entrypoint_builds_graph_from_main_module():
    script = str(Path(supervisor_module.__file__).resolve())
    probe = """
import asyncio
import pathlib
import sys
script = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(script.parent))
def check_graph(coroutine):
    assert sys.modules["__main__"].SupervisorLoop().graph is not None
    coroutine.close()
asyncio.run = check_graph
exec(compile(script.read_text(), str(script), "exec"), sys.modules["__main__"].__dict__)
"""
    subprocess.run([sys.executable, "-c", probe, script], check=True, capture_output=True, text=True)


def test_durable_claim_recovery_and_eight_total_attempt_cap(tmp_path):
    db = task_database(str(tmp_path / "tasks.sqlite3"))
    try:
        db.execute("INSERT INTO tasks(task_id,repo,issue,status,payload) VALUES(?,?,?,?,?)",
                   ("task", "org/repo", 1, "queued", '{"task_id":"task"}'))
        claimed = claim_queued_task(db, "supervisor-a", now=1000)
        assert claimed["attempts"] == 1 and claimed["claim_owner"] == "supervisor-a"
        assert claim_queued_task(db, "supervisor-b", now=1100) is None
        recovered = claim_queued_task(db, "supervisor-b", now=2000, lease_seconds=900)
        assert recovered["attempts"] == 1 and recovered["claim_owner"] == "supervisor-b"
        db.execute("UPDATE tasks SET status='queued', attempts=8, claimed_at=0 WHERE task_id='task'")
        exhausted = claim_queued_task(db, "supervisor-c", now=4000, lease_seconds=900)
        assert exhausted["status"] == "running" and exhausted["attempts"] == 8
    finally:
        db.close()


@pytest.mark.parametrize("runtime,expected_host", [
    (None, "current.example.com"),
    ({"acceptor_url": "https://pinned.example.com"}, "pinned.example.com"),
])
def test_completion_url_uses_pinned_runtime_or_current_fallback(monkeypatch, runtime, expected_host):
    monkeypatch.setenv("SUPERVISOR_AUTH_TOKEN", "supervisor-test-only")
    monkeypatch.setenv("ACCEPTOR_URL", "https://current.example.com")
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"{}"

    def urlopen(request, timeout):
        requests.append(request)
        assert timeout == 30
        return Response()

    supervisor_module._candidate_workflow.complete_via_gateway(
        "task", "failed", runtime, {"error": "test-failure"}, urlopen)
    assert requests[0].full_url == f"https://{expected_host}/api/v1/supervisor/tasks/task"
    assert requests[0].method == "POST"


def test_catalog_failure_consumes_eight_attempts_then_waits_for_lease(monkeypatch, tmp_path):
    path = str(tmp_path / "tasks.sqlite3")
    db = task_database(path)
    db.execute("INSERT INTO tasks(task_id,status,payload) VALUES(?,?,?)",
               ("task", "queued", '{"repository_auth":"github-app"}'))
    db.close()
    monkeypatch.setattr(supervisor_module, "task_database", lambda: task_database(path))

    def deny_catalog(_task):
        raise RuntimeError("kubernetes-api-http-403")

    completions = []

    def fail_completion(task_id, status, runtime, **_details):
        completions.append((task_id, status, runtime))
        raise supervisor_module.urllib.error.URLError("Name or service not known")

    monkeypatch.setattr(supervisor_module, "_load_pinned_catalog", deny_catalog)
    monkeypatch.setattr(supervisor_module, "_complete_via_gateway", fail_completion)
    loop = supervisor_module.SupervisorLoop()
    for _attempt in range(8):
        assert asyncio.run(loop.process_next_durable_task()) is True
    assert asyncio.run(loop.process_next_durable_task()) is False
    assert completions == [("task", "failed", None)]
    db = task_database(path)
    try:
        task = db.execute("SELECT * FROM tasks WHERE task_id='task'").fetchone()
        assert task["attempts"] == 8
        assert task["status"] == "running"
        assert task["claim_owner"] and task["claimed_at"]
    finally:
        db.close()


def test_pinned_catalog_is_schema_validated_and_role_tools_are_bounded(monkeypatch):
    chart = Path(__file__).resolve().parents[2] / "helm/orchestrator/files"
    content = (chart / "agent-catalog.yaml").read_text()
    schema = (chart / "agent-catalog.schema.json").read_text()
    runtime = json.dumps({"acceptor_url": "https://gateway", "coder_url": "http://p40-0/v1",
                          "coder_model": "coder-model", "coder_native_download": False,
                          "judge_url": "http://p40-1/v1", "judge_model": "judge-model",
                          "threshold": 0.8, "max_total_attempts": 8,
                          "allowed_base_branches": "main", "reviewers": "reviewer",
                          "repo_policies": {},
                          "worker_parallelism": 1}, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256((content + schema + runtime).encode()).hexdigest()
    monkeypatch.setattr(supervisor_module, "_k8s_request", lambda *_args: {
        "data": {"agent-roles.yaml": content, "agent-roles.schema.json": schema,
                 "runtime-config.json": runtime}})
    task = {"config_hash": digest, "config_version": "1"}
    assert supervisor_module._load_pinned_catalog(task)[0]["roles"]["coder"]["endpoint_alias"] == "coder"

    invalid = content.replace("spec_version: 1", "spec_version: 2")
    monkeypatch.setattr(supervisor_module, "_k8s_request", lambda *_args: {
        "data": {"agent-roles.yaml": invalid, "agent-roles.schema.json": schema,
                 "runtime-config.json": runtime}})
    task["config_hash"] = hashlib.sha256((invalid + schema + runtime).encode()).hexdigest()
    with pytest.raises(RuntimeError, match="task-catalog-schema-invalid"):
        supervisor_module._load_pinned_catalog(task)


def test_judge_calls_configured_p40_1_endpoint(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"score":0.9,"review":"valid"}'}}]}

    class FakeClient:
        def __init__(self, timeout):
            assert timeout == 90

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()

    monkeypatch.setattr(supervisor_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("JUDGE_URL", "http://p40-1.inference.svc:8000/v1")
    monkeypatch.setenv("JUDGE_MODEL", "qwen-judge-32k")
    monkeypatch.setenv("OPENAI_API_KEY", "judge-key")
    task = {"payload": '{"task_id":"task"}'}
    candidate = {"status": "candidate", "validation": {"status": "passed"}, "patch": "diff"}
    score, review = asyncio.run(supervisor_module._judge_candidate(task, candidate))
    assert score == 0.9 and review == "valid"
    assert calls[0][0] == "http://p40-1.inference.svc:8000/v1/chat/completions"
    assert calls[0][1]["json"]["model"] == "qwen-judge-32k"


@pytest.mark.parametrize("operation", ["judge", "plan"])
@pytest.mark.parametrize("initial", [None, "incomplete", "downloading", "unloaded", "loaded", "disabled"])
def test_supervisor_native_model_preparation(monkeypatch, operation, initial):
    httpx = supervisor_module.httpx
    calls = []
    model = "owner/judge-GGUF:Q4_K_M"
    statuses = iter([initial, "unloaded"])
    monkeypatch.setenv("OPENAI_API_KEY", "judge-key")
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    def transport(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer judge-key"
        if request.url.path == "/models":
            assert initial != "disabled"
            if request.method == "POST":
                assert json.loads(request.content) == {"model": model}
                return httpx.Response(200, json={"success": True})
            status = next(statuses)
            return httpx.Response(200, json={"data": [] if status is None else [
                {"id": model, "status": {"value": status}},
            ]})
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content)["model"] == model
        return httpx.Response(200, json={"choices": [{"message": {
            "content": '{"score":0.9,"review":"valid","plan":"implement fix"}',
        }}]})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(transport), **kwargs))
    runtime = {"judge_url": "http://judge/v1", "judge_model": model,
               "judge_native_download": initial != "disabled"}
    task = {"payload": '{"task_id":"task"}'}
    if operation == "judge":
        candidate = {"validation": {"status": "passed"}, "patch": "diff"}
        assert asyncio.run(supervisor_module._judge_candidate(task, candidate, runtime)) == (0.9, "valid")
    else:
        assert asyncio.run(supervisor_module._plan_task(task, {"roles": {"coder": {}}}, runtime)) == "implement fix"
    assert sum(request.method == "POST" and request.url.path == "/models" for request in calls) == (initial in (None, "incomplete"))
    assert calls[-1].url.path == "/v1/chat/completions"


@pytest.mark.parametrize("operation", ["judge", "plan"])
@pytest.mark.parametrize("failure", ["download", "status", "timeout", "concurrent"])
def test_supervisor_native_model_failures(monkeypatch, operation, failure):
    httpx = supervisor_module.httpx
    monkeypatch.setenv("OPENAI_API_KEY", "judge-key")
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=TimeoutError if failure == "timeout" else None))
    responses = iter([
        httpx.Response(200, json={"data": []}),
        httpx.Response(400, json={"error": {"message": "is already downloading" if failure == "concurrent" else "bad HF repo"}}),
        httpx.Response(200, json={"data": [{"id": "judge-model", "status": {"value": "unloaded"}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": '{"score":0.9,"plan":"implement fix"}'}}]}),
    ] if failure in ("download", "concurrent") else [
        httpx.Response(200, json={"data": [{"id": "judge-model", "status": {
            "value": "downloading", "failed": failure == "status",
        }}]}),
    ])
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(lambda request: next(responses)), **kwargs))
    runtime = {"judge_url": "http://judge/v1", "judge_model": "judge-model", "judge_native_download": True}
    task = {"payload": '{"task_id":"task"}'}
    if operation == "judge":
        pending = supervisor_module._judge_candidate(task, {"validation": {"status": "passed"}}, runtime)
    else:
        pending = supervisor_module._plan_task(task, {"roles": {"coder": {}}}, runtime)
    if failure == "concurrent":
        asyncio.run(pending)
    else:
        with pytest.raises({"download": httpx.HTTPStatusError, "status": RuntimeError, "timeout": TimeoutError}[failure]):
            asyncio.run(pending)


def test_pr_policy_fails_closed_and_publication_never_merges(monkeypatch):
    task = {"repo": "org/repo", "task_id": "task-123", "issue": 7}
    candidate = {"base_branch": "main", "base_sha": "base-sha", "edits": [
        {"path": "src/fix.py", "content": "fixed = True\n"}]}
    request_paths = []
    def github(method, repo, path, body=None):
        request_paths.append((method, path))
        if method == "GET" and path == "/git/ref/heads/main":
            return {"object": {"sha": "base-sha"}}
        if method == "GET" and path == "/git/commits/base-sha":
            return {"tree": {"sha": "base-tree"}}
        if method == "POST" and path == "/git/trees":
            return {"sha": "new-tree"}
        if method == "GET" and path.startswith("/git/ref/heads/"):
            return None
        if method == "POST" and path == "/git/commits":
            return {"sha": "new-commit"}
        if method == "GET" and path.startswith("/pulls?"):
            return []
        if method == "POST" and path == "/pulls":
            return {"number": 10, "html_url": "https://github.com/org/repo/pull/10",
                    "state": "open", "base": {"ref": "main"}}
        return {}

    monkeypatch.setattr(supervisor_module, "_task_github", lambda *_args: github)
    monkeypatch.delenv("PR_ALLOWED_BASE_BRANCHES", raising=False)
    monkeypatch.delenv("PR_REVIEWERS", raising=False)
    with pytest.raises(RuntimeError, match="pr-policy-required"):
        supervisor_module._publish_candidate(task, candidate)
    assert request_paths == []

    monkeypatch.setenv("PR_ALLOWED_BASE_BRANCHES", "main")
    monkeypatch.setenv("PR_REVIEWERS", "reviewer")
    assert supervisor_module._publish_candidate(task, candidate) == "https://github.com/org/repo/pull/10"
    assert ("POST", "/pulls/10/requested_reviewers") in request_paths
    assert not any("/merge" in path for _method, path in request_paths)


@pytest.mark.parametrize("obsolete_setting", [None, "REPO_GITHUB_PAT", "CODEFACTORY_AUTH_TOKEN", "PR_GITHUB_TOKEN"])
def test_dispatch_injects_only_task_scoped_credentials_and_pinned_catalog(monkeypatch, obsolete_setting):
    catalog_hash = "a" * 64
    seed = {"spec": {"suspend": True, "template": {"spec": {
        "containers": [{"name": "coder", "env": [
            {"name": "JUDGE_URL", "value": "judge"},
        ]}], "volumes": [{"name": "agent-catalog", "configMap": {"name": "old"}}]}}}}
    if obsolete_setting:
        seed["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": obsolete_setting, "value": "stale-test-only"})
    requests = []

    def api(method, path, body=None):
        requests.append((method, path, body))
        if method == "GET" and path.endswith("/code-factory-worker"):
            return seed
        if method == "POST":
            return {"metadata": {"name": body["metadata"]["name"]}}
        return {"data": {}}

    monkeypatch.setattr(supervisor_module, "_k8s_request", api)
    monkeypatch.setenv("AGENT_CATALOG_NAME_PREFIX", "code-factory-agent-catalog-")
    task = {"task_id": "issue-123", "attempts": 1, "config_hash": catalog_hash,
            "config_version": "1", "payload": '{"repo":"org/repo","repository_auth":"github-app"}', "plan": "test plan",
            "repo": "org/repo", "credential_ref": "task-credential-issue-123"}
    runtime = {"coder_url": "http://coder", "coder_model": "coder-model", "coder_native_download": False,
               "acceptor_url": "https://gateway", "repo_policies": {}}
    if obsolete_setting:
        with pytest.raises(RuntimeError, match="^task-authentication-unsupported$"):
            supervisor_module._dispatch_task(task, runtime)
        assert not any(method == "POST" for method, _path, _body in requests)
        return
    name = supervisor_module._dispatch_task(task, runtime)
    assert name == "code-factory-worker-issue-123-1"
    job = requests[-1][2]
    env = {entry["name"]: entry for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "CODEFACTORY_AUTH_TOKEN" not in env and "JUDGE_URL" not in env
    assert "ALLOWED_REPOS" not in env and "ALLOWED_REPO" not in env
    assert env["TASK_CALLBACK_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == task["credential_ref"]
    assert env["TASK_REPOSITORY_AUTH"]["valueFrom"]["secretKeyRef"]["key"] == "repository-auth"
    assert json.loads(env["ASSIGNED_TASK_PAYLOAD"]["value"])["supervisor_plan"] == "test plan"
    assert job["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"].endswith(catalog_hash[:12])


@pytest.mark.parametrize("payload", [{}, {"repository_auth": "pat"}])
def test_old_tasks_cannot_obtain_publication_client(payload):
    with pytest.raises(RuntimeError, match="^task-authentication-unsupported$"):
        supervisor_module._task_github({"repo": "org/repo", "payload": json.dumps(payload)})


@pytest.mark.parametrize("setting", ["PR_GITHUB_TOKEN", "REPO_GITHUB_PAT", "GITHUB_TOKEN", "CODEFACTORY_AUTH_TOKEN",
                                     "GITHUB_AUTH_MODE", "CODEFACTORY_AUTH_MODE"])
def test_supervisor_rejects_removed_auth_settings(monkeypatch, setting):
    monkeypatch.setenv(setting, "stale-test-only")
    with pytest.raises(ValueError, match="^repository-authentication-configuration-invalid$"):
        supervisor_module.SupervisorLoop()


def test_supervisor_reconstructs_candidate_diff_and_rejects_patch_mismatch(monkeypatch):
    def github(method, _repo, path, _body=None):
        if path == "/git/commits/" + "a" * 40:
            return {"tree": {"sha": "tree-sha"}}
        if path == "/git/trees/tree-sha?recursive=1":
            return {"truncated": False, "tree": []}
        raise AssertionError(path)

    monkeypatch.setattr(supervisor_module, "_task_github", lambda *_args: github)
    task = {"task_id": "task", "repo": "org/repo", "issue": 1}
    candidate = {"base_sha": "a" * 40, "changed_files": ["src/fix.py"],
                 "edits": [{"path": "src/fix.py", "content": "value = 1\n"}],
                 "patch": "untrusted worker patch"}
    with pytest.raises(RuntimeError, match="candidate-patch-edits-mismatch"):
        supervisor_module._verified_candidate_patch(task, candidate)

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
