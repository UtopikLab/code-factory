"""
Unit tests for the worker sidecar (worker.py).

Covers the pure helpers (edit extraction, task-id extraction, patch rendering),
the CodeEditor file writer, and the heuristic self-review fallback.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import worker
from prometheus_client import CollectorRegistry
from worker import (
    CodeEditor,
    _extract_edits,
    _extract_task_id,
    _failure_result,
    render_patch,
)


@pytest.fixture(autouse=True)
def gateway_auth(monkeypatch):
    monkeypatch.setenv("TASK_CALLBACK_TOKEN", "callback-test-secret")
    monkeypatch.setenv("TASK_REPOSITORY_AUTH", "github-app")
    monkeypatch.delenv("CODER_NATIVE_DOWNLOAD", raising=False)
    monkeypatch.delenv("JUDGE_NATIVE_DOWNLOAD", raising=False)


def test_app_repository_credentials_refresh_and_stay_read_only(monkeypatch):
    monkeypatch.setenv("TASK_REPOSITORY_AUTH", "github-app")
    monkeypatch.setenv("TASK_REPOSITORY", "org/repo")
    monkeypatch.setenv("ASSIGNED_TASK_ID", "task-123")
    monkeypatch.setenv("ACCEPTOR_URL", "https://gateway.test")
    now = [1000]
    monkeypatch.setattr(worker.time, "time", lambda: now[0])
    refreshes = []

    def transport(request):
        if request.url.host == "gateway.test":
            assert request.headers["X-Task-Token"] == "callback-test-secret"
            assert "Authorization" not in request.headers
            refreshes.append(request)
            return httpx.Response(200, json={"repo": "org/repo", "token": "renewal-" + str(len(refreshes)),
                                             "expires_at": now[0] + 3600})
        assert request.headers["Authorization"] == "Bearer renewal-" + str(len(refreshes))
        return httpx.Response(200, json={"ok": True})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            github = worker.GitHub(client, "org/repo")
            await github.call("GET", "/contents")
            await github.call("GET", "/contents")
            assert len(refreshes) == 1
            now[0] += 3550
            await github.call("GET", "/contents")
            assert len(refreshes) == 2
            with pytest.raises(worker.TaskFailure, match="read-only"):
                await github.call("POST", "/contents")
    asyncio.run(check())


def test_app_broker_redirect_is_not_followed(monkeypatch):
    monkeypatch.setenv("TASK_REPOSITORY_AUTH", "github-app")
    monkeypatch.setenv("TASK_REPOSITORY", "org/repo")
    monkeypatch.setenv("ASSIGNED_TASK_ID", "task-123")
    monkeypatch.setenv("ACCEPTOR_URL", "https://gateway.test")
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://attacker.test"})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            with pytest.raises(worker.TaskFailure, match="^repository-broker-unavailable$"):
                await worker.GitHub(client, "org/repo").call("GET", "/contents")
    asyncio.run(check())
    assert len(calls) == 1


@pytest.mark.parametrize("initial", [None, "incomplete", "downloading", "unloaded", "loaded"])
def test_native_model_preparation(monkeypatch, initial):
    model = "owner/new-model-GGUF:Q4_K_M"
    calls = []
    statuses = iter([initial, "downloaded", "unloaded"])
    monkeypatch.setenv("CODER_API_KEY", "model-test-secret")
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())
    def transport(request):
        calls.append(request)
        assert request.url == "http://model/models"
        assert request.headers["Authorization"] == "Bearer model-test-secret"
        if request.method == "POST":
            assert json.loads(request.content) == {"model": model}
            return httpx.Response(200, json={"success": True})
        status = next(statuses)
        return httpx.Response(200, json={"data": [] if status is None else [
            {"id": model, "status": {"value": status}},
        ]})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(transport), **kwargs))
    asyncio.run(worker._prepare_native_model("http://model/v1/", model, True))
    assert sum(request.method == "POST" for request in calls) == (initial in (None, "incomplete"))


def test_native_model_preparation_disabled(monkeypatch):
    monkeypatch.setattr(worker.httpx, "AsyncClient", Mock(side_effect=AssertionError("No native API expected")))
    asyncio.run(worker._prepare_native_model("https://external/v1", "custom-model", False))


@pytest.mark.parametrize("failure", ["download", "status", "concurrent", "timeout"])
def test_native_model_preparation_failures(monkeypatch, failure):
    monkeypatch.setenv("CODER_API_KEY", "model-test-secret")
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock(side_effect=TimeoutError if failure == "timeout" else None))
    responses = iter([
        httpx.Response(200, json={"data": []}),
        httpx.Response(400, json={"error": {"message": "is already downloading" if failure == "concurrent" else "bad HF repo"}}),
        httpx.Response(200, json={"data": [{"id": "owner/repo:Q4_K_M", "status": {"value": "unloaded"}}]}),
    ] if failure in ("download", "concurrent") else [
        httpx.Response(200, json={"data": [{"id": "owner/repo:Q4_K_M", "status": {
            "value": "downloading", "failed": failure == "status",
        }}]}),
    ])
    client_class = httpx.AsyncClient
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(lambda request: next(responses)), **kwargs))
    async def prepare():
        await worker._prepare_native_model("http://model/v1", "owner/repo:Q4_K_M", True)
    if failure == "concurrent":
        asyncio.run(prepare())
    else:
        with pytest.raises({"download": httpx.HTTPStatusError, "status": worker.TaskFailure,
                            "timeout": TimeoutError}[failure]):
            asyncio.run(prepare())


@pytest.mark.parametrize("status", ["success", "failed", "completed"])
def test_record_worker_metrics_with_string_status(monkeypatch, status):
    registry = CollectorRegistry()
    for name in ("TASKS", "CODE_CHANGES", "JOB_DURATION", "LAST_STATUS"):
        original = getattr(worker, name)
        metric = type(original)(
            original._name,
            original._documentation,
            labelnames=original._labelnames,
            registry=registry,
        )
        monkeypatch.setattr(worker, name, metric)
    monkeypatch.setattr(worker.time, "monotonic", lambda: 110.0)

    asyncio.run(worker._record_worker_metrics(
        "metrics-task", {"status": status}, changed=2, job_start=100.0,
    ))

    assert registry.get_sample_value("worker_last_status", {"status": status}) == 1
    assert registry.get_sample_value("worker_tasks_total", {"status": status}) == 1
    assert registry.get_sample_value("worker_code_changes_total") == 2
    assert registry.get_sample_value("worker_job_duration_seconds_count") == 1
    assert registry.get_sample_value("worker_job_duration_seconds_sum") == 10.0


# --------------------------------------------------------------------------- #
# Edit extraction
# --------------------------------------------------------------------------- #


def test_extract_edits_from_files_key():
    payload = {"files": [{"path": "a.py", "content": "x"}]}
    assert _extract_edits(payload) == [{"path": "a.py", "content": "x"}]


def test_extract_edits_from_edits_key():
    payload = {"edits": [{"path": "b.py", "content": "y"}]}
    assert _extract_edits(payload) == [{"path": "b.py", "content": "y"}]


def test_extract_edits_from_changes_key():
    payload = {"changes": [{"path": "c.py", "content": "z"}]}
    assert _extract_edits(payload) == [{"path": "c.py", "content": "z"}]


def test_extract_edits_returns_empty_for_non_dict():
    assert _extract_edits("not a dict") == []


def test_extract_edits_returns_empty_when_no_supported_key():
    assert _extract_edits({"foo": [{"path": "x", "content": "y"}]}) == []


# --------------------------------------------------------------------------- #
# Task id extraction
# --------------------------------------------------------------------------- #


def test_extract_task_id_from_task_id_field():
    body = json.dumps({"task_id": "abc-123"})
    assert _extract_task_id(body.encode("utf-8")) == "abc-123"


def test_extract_task_id_from_id_field():
    body = json.dumps({"id": "xyz"})
    assert _extract_task_id(body.encode("utf-8")) == "xyz"


@pytest.mark.parametrize("key", ["task_id", "id"])
@pytest.mark.parametrize("value", [0, 42, -7, 0.0, 12.5])
def test_extract_task_id_from_numeric_field(key, value):
    body = json.dumps({key: value})
    assert _extract_task_id(body.encode("utf-8")) == str(value)


def test_extract_task_id_prefers_numeric_task_id():
    body = json.dumps({"task_id": 42, "id": "xyz"})
    assert _extract_task_id(body.encode("utf-8")) == "42"


def test_extract_task_id_empty_when_missing():
    body = json.dumps({"foo": "bar"})
    assert _extract_task_id(body.encode("utf-8")) == ""


def test_extract_task_id_empty_body():
    assert _extract_task_id(b"") == ""


# --------------------------------------------------------------------------- #
# Failure result
# --------------------------------------------------------------------------- #


def test_failure_result_shape():
    result = _failure_result("spawn-1", "boom")
    assert result == {"task_id": "spawn-1", "error": "boom"}


# --------------------------------------------------------------------------- #
# Patch rendering
# --------------------------------------------------------------------------- #


def test_render_patch_basic():
    changed = ["a.py"]
    contents = [("a.py", "def f():\n    return 1")]
    patch = render_patch(changed, contents)
    assert patch == "diff --worker\n--- a.py\n+++ a.py\n\nnew-file-mode +0000\nold-file-mode -0000\n\ndef f():\n    return 1"


def test_render_patch_multiple_files():
    changed = ["a.py", "b.py"]
    contents = [
        ("a.py", "A"),
        ("b.py", "B"),
    ]
    patch = render_patch(changed, contents)
    assert "diff --worker" in patch
    assert "new-file-mode +0000" in patch
    assert "A" in patch and "B" in patch


# --------------------------------------------------------------------------- #
# CodeEditor
# --------------------------------------------------------------------------- #


def test_write_creates_file(tmp_path):
    editor = CodeEditor(str(tmp_path / "repo"))
    assert editor.working_dir == str(tmp_path / "repo")
    assert editor._write("a.py", "hello") == "hello"
    assert (tmp_path / "repo" / "a.py").read_text() == "hello"


def test_write_creates_nested_dirs(tmp_path):
    editor = CodeEditor(str(tmp_path / "repo"))
    editor._write("sub/dir/file.txt", "data")
    assert (tmp_path / "repo" / "sub" / "dir" / "file.txt").read_text() == "data"


@pytest.mark.parametrize("path", ["/tmp/escape", "../escape", "src/../../escape", ".github/workflows/run.yaml", ".env", "pyproject.toml", "src\\escape"])
def test_editor_rejects_unsafe_paths(tmp_path, path):
    editor = CodeEditor(str(tmp_path / "repo"))
    with pytest.raises(ValueError, match="edit-path"):
        editor._write(path, "unsafe")


def test_editor_rejects_symlink(tmp_path):
    editor = CodeEditor(str(tmp_path / "repo"))
    (tmp_path / "repo" / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe-edit-path"):
        editor._write("link/escape", "unsafe")


def test_write_returns_none_on_oserror(tmp_path):
    # Writing into an existing directory raises OSError (IsADirectoryError).
    editor = CodeEditor(str(tmp_path / "repo"))
    (tmp_path / "repo" / "locked").mkdir()
    result = editor._write("locked", "content")
    assert result is None


def test_apply_edits_returns_patch(tmp_path):
    editor = CodeEditor(str(tmp_path / "repo"))
    edits = [{"path": "a.py", "content": "x=1"}, {"path": "b.py", "content": "y=2"}]
    output, changed, contents = editor.apply_edits(edits)
    assert changed == ["a.py", "b.py"]
    assert contents == [("a.py", "x=1"), ("b.py", "y=2")]
    assert "x=1" in output and "y=2" in output


def test_apply_edits_skips_empty_edits(tmp_path):
    editor = CodeEditor(str(tmp_path / "repo"))
    edits = [{"path": "", "content": "x"}, {"path": "a.py", "content": ""}]
    _output, changed, contents = editor.apply_edits(edits)
    assert changed == []
    assert contents == []


def test_apply_edits_skips_malformed_edit(tmp_path):
    editor = CodeEditor(str(tmp_path / "repo"))
    edits = ["not a dict", {"path": "a.py", "content": "ok"}]
    _output, changed, contents = editor.apply_edits(edits)
    assert changed == ["a.py"]
    assert contents == [("a.py", "ok")]


@pytest.mark.parametrize("edit_key", ["files", "edits"])
def test_gateway_job_to_assigned_worker(monkeypatch, tmp_path, edit_key):
    payload = {
        "name": "orchestrator-agent", "ref": "refs/heads/main",
        "payload": {
            "issue_number": 2, "issue_title": "Update greeting", "repo": "org/repo",
            "issue_url": "https://github.com/org/repo/issues/2", "default_branch": "main",
            edit_key: [{"path": "src/greeting.py", "content": "greeting = 'hello'\n"}],
        },
    }
    task_id = "issue-" + "a" * 32
    payload["task_id"] = task_id
    monkeypatch.setenv("TASK_CALLBACK_TOKEN", "callback-test-secret")
    monkeypatch.setenv("TASK_WORK_DIR", str(tmp_path))
    client = AsyncMock()
    client.post.return_value = httpx.Response(200, json={"task_id": task_id, "status": "candidate"})
    result = asyncio.run(worker._run_worker(client, "https://acceptor", "worker", payload, task_id))
    assert result["task_id"] == task_id
    assert "error" not in result
    assert result["status"] == "candidate"
    assert "greeting = 'hello'" in result["patch"]
    assert result["validation"]["status"] == "not-run"
    assert "score" not in result and "pr_url" not in result
    assert not list(tmp_path.glob("task-*"))
    client.post.assert_awaited_once()
    assert "Authorization" not in client.post.call_args.kwargs["headers"]


@pytest.mark.parametrize(("task_id", "payload", "error"), [
    ("2", "{", "invalid-assigned-task-json"),
    ("2", "[]", "invalid-assigned-task-payload"),
    ("2", "{}", "invalid-assigned-task-payload"),
    ("2", '{"payload": []}', "invalid-assigned-task-payload"),
    ("", '{"repo": "org/repo"}', "invalid-assigned-task-id"),
    ("unknown", '{"repo": "org/repo"}', "invalid-assigned-task-id"),
    ("../2", '{"repo": "org/repo"}', "invalid-assigned-task-id"),
    ("2", '{"payload": {"issue_number": 3}}', "assigned-task-id-mismatch"),
    (None, '{"repo": "org/repo"}', "assigned-task-id-and-payload-required"),
    ("2", None, "assigned-task-id-and-payload-required"),
])
def test_malformed_assignment_fails_before_io(monkeypatch, task_id, payload, error):
    for name, value in (("ASSIGNED_TASK_ID", task_id), ("ASSIGNED_TASK_PAYLOAD", payload)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    client = Mock(side_effect=AssertionError("No I/O expected"))
    monkeypatch.setattr(worker.httpx, "AsyncClient", client)
    with pytest.raises(ValueError, match=error):
        asyncio.run(worker.main_async())
    client.assert_not_called()


@pytest.mark.parametrize("legacy_value", ["", "*", "another/repo"])
def test_issue_identity_does_not_require_repository_allowlist(monkeypatch, legacy_value):
    monkeypatch.setenv("ALLOWED_REPOS", legacy_value)
    monkeypatch.setenv("ALLOWED_REPO", legacy_value)
    assert worker._issue_identity({"repo": "new/project", "issue_number": 2}) == ("new/project", 2)


def test_assigned_issue_reports_unsupported_discovery(monkeypatch, tmp_path):
    client = AsyncMock()
    client.post.return_value = httpx.Response(200, json={"task_id": "2", "status": "candidate_failed"})
    monkeypatch.setenv("TASK_REPO_POLICIES", "{}")
    monkeypatch.setenv("TASK_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(worker, "_implement_issue", AsyncMock(side_effect=worker.TaskFailure("automatic-validation-unsupported")))
    payload = {"payload": {"issue_number": 2, "repo": "org/repo", "issue_title": "Fix bug"}}
    result = asyncio.run(worker._run_worker(client, "https://acceptor", "worker", payload, "2"))
    assert result["task_id"] == "2"
    assert result["status"] == "candidate_failed"
    assert result["error"] == "automatic-validation-unsupported"
    client.get.assert_not_awaited()
    client.post.assert_awaited_once_with(
        "https://acceptor/api/v1/tasks/2", headers={"Content-Type": "application/json",
                       "X-Task-Token": "callback-test-secret"},
        json=result, follow_redirects=False,
    )


def test_worker_contract_is_candidate_only_and_github_is_read_only(monkeypatch):
    assert not hasattr(worker, "judge_review")
    assert not hasattr(worker, "_publish")
    github = worker.GitHub.__new__(worker.GitHub)
    with pytest.raises(worker.TaskFailure, match="worker-github-read-only"):
        asyncio.run(github.call("POST", "/pulls", {}))


@pytest.mark.parametrize("supervisor_plan", [None, "Update the app and validate the change"])
def test_issue_implementation_returns_candidate_without_judging_or_publishing(monkeypatch, tmp_path, supervisor_plan):
    monkeypatch.setenv("TASK_REPO_POLICIES", json.dumps({"org/repo": {
        "context_files": ["src/app.py"], "editable_files": ["src/app.py"],
        "validation_argv": ["/usr/local/bin/python", "-m", "pytest"],
        "validation_timeout": 30,
    }}))

    class ReadOnlyGitHub:
        def __init__(self, _client, _repo):
            pass

        async def call(self, _method, path):
            if path == "":
                return {"full_name": "org/repo", "default_branch": "main"}
            if path == "/issues/3":
                return {"number": 3, "title": "Fix app", "body": "Issue details",
                        "html_url": "https://github.com/org/repo/issues/3"}
            if path == "/git/ref/heads/main":
                return {"object": {"sha": "base-sha"}}
            raise AssertionError(f"unexpected GitHub path: {path}")

    async def snapshot(_github, editor, _sha):
        editor._write("src/app.py", "value = 1\n")

    monkeypatch.setattr(worker, "GitHub", ReadOnlyGitHub)
    monkeypatch.setattr(worker, "_snapshot", snapshot)
    monkeypatch.setattr(worker, "_generate_edits", AsyncMock(return_value=[
        {"path": "src/app.py", "content": "value = 2\n"}]))
    monkeypatch.setattr(worker, "_run_validation", AsyncMock(return_value="1 passed"))
    payload = {"repo": "org/repo", "payload": {"repo": "org/repo", "issue_number": 3}}
    if supervisor_plan is not None:
        payload["supervisor_plan"] = supervisor_plan
    candidate = asyncio.run(worker._implement_issue(None, payload, "task-3", str(tmp_path)))
    assert worker._generate_edits.await_args.args[3] == (supervisor_plan or "")
    assert candidate["status"] == "candidate"
    assert candidate["validation"]["status"] == "passed"
    assert candidate["base_branch"] == "main" and candidate["base_sha"] == "base-sha"
    assert candidate["edits"] == [{"path": "src/app.py", "content": "value = 2\n"}]
    assert "score" not in candidate and "pr_url" not in candidate


@pytest.mark.parametrize("supervisor_plan", ["", "Update the app and validate the change"])
def test_generate_edits_includes_supervisor_plan(monkeypatch, supervisor_plan):
    monkeypatch.setenv("CODER_API_KEY", "model-test-secret")
    monkeypatch.setenv("CODER_URL", "https://model/v1")
    monkeypatch.setenv("CODER_MODEL", "test-model")
    edits = [{"path": "src/app.py", "content": "value = 2\n"}]
    coder = AsyncMock()
    coder.__aenter__.return_value = coder
    coder.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=json.dumps({"edits": edits})), finish_reason="stop")])
    monkeypatch.setattr(worker.openai, "AsyncOpenAI", Mock(return_value=coder))
    context = {"src/app.py": "value = 1\n"}
    policy = {"editable_files": ["src/app.py"]}
    result = asyncio.run(worker._generate_edits({"title": "Fix app"}, context, policy, supervisor_plan))
    prompt = json.loads(coder.chat.completions.create.await_args.kwargs["messages"][1]["content"])
    assert prompt["plan"] == supervisor_plan
    assert prompt["files"] == context
    assert result == edits


@pytest.mark.parametrize("repo", ["first/project", "another/backend"])
def test_automatic_policy_needs_no_repository_entry(monkeypatch, tmp_path, repo):
    monkeypatch.setenv("TASK_REPO_POLICIES", "{}")
    files = {
        "engine/app/__init__.py": "",
        "engine/app/main.py": "value = 1\n",
        "engine/tests/test_health.py": "def test_health(): assert True\n",
        "engine/tests/helper.py": "value = 2\n",
        "engine/pyproject.toml": "[project]\n",
        ".github/workflows/ci.yml": "name: protected\n",
    }
    for path, content in files.items():
        full = tmp_path / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    policy = worker._repo_policy(repo, str(tmp_path))
    assert policy["editable_files"] == ["engine/app/__init__.py", "engine/app/main.py"]
    assert "engine/tests/test_health.py" in policy["context_files"]
    command = policy["validation_argv"]
    assert command[:2] == ["/usr/local/bin/python", "-c"]
    assert "/workspace/engine" in command[2]
    assert "engine/tests/test_health.py" in command[2]
    assert "no:cacheprovider" in command[2]
    assert ".github/workflows/ci.yml" not in policy["context_files"]


def test_automatic_policy_rejects_repository_without_tests(monkeypatch, tmp_path):
    monkeypatch.setenv("TASK_REPO_POLICIES", "{}")
    (tmp_path / "app.py").write_text("value = 1\n")
    with pytest.raises(worker.TaskFailure, match="automatic-validation-unsupported"):
        worker._repo_policy("any/repository", str(tmp_path))


def test_legacy_worker_requires_assignment(monkeypatch, tmp_path):
    requests = []

    def acceptor(request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/spawn"):
            assert json.loads(request.content) == {"worker_id": "legacy", "payload": {"files": [{"path": "a.py", "content": "x=1"}]}}
            return httpx.Response(202, json={"task_id": 2})
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "2", "status": "completed", "lifecycle": "synced"})
        return httpx.Response(200, json={})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(acceptor), **kwargs))
    monkeypatch.setenv("TASK_WORK_DIR", str(tmp_path))
    monkeypatch.delenv("CODER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="gateway-assignment-required"):
        asyncio.run(worker.run_worker("https://acceptor", "legacy", {"files": [{"path": "a.py", "content": "x=1"}]}))
    assert requests == []
    assert not list(tmp_path.glob("task-*"))


def test_repository_client_has_no_global_token_fallback(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-test-only")
    monkeypatch.delenv("REPO_GITHUB_PAT", raising=False)
    with pytest.raises(worker.TaskFailure, match="repository-authentication-unsupported"):
        worker.GitHub(None, "org/repo")


@pytest.mark.parametrize("setting", ["REPO_GITHUB_PAT", "PR_GITHUB_TOKEN", "CODEFACTORY_AUTH_TOKEN",
                                     "GITHUB_AUTH_MODE", "CODEFACTORY_AUTH_MODE"])
def test_worker_rejects_removed_repository_auth_settings(monkeypatch, setting):
    monkeypatch.setenv(setting, "stale-test-only")
    monkeypatch.setenv("TASK_REPOSITORY", "org/repo")
    with pytest.raises(worker.TaskFailure, match="^repository-authentication-unsupported$"):
        worker.GitHub(None, "org/repo")


@pytest.mark.parametrize("marker", [None, "pat", ""])
def test_worker_rejects_old_task_credentials(monkeypatch, marker):
    if marker is None:
        monkeypatch.delenv("TASK_REPOSITORY_AUTH")
    else:
        monkeypatch.setenv("TASK_REPOSITORY_AUTH", marker)
    with pytest.raises(worker.TaskFailure, match="^repository-broker-required$"):
        worker.GitHub(None, "org/repo")


@pytest.mark.parametrize("status,reason", [(401, "repository-credential-denied"),
                                         (403, "repository-credential-denied"),
                                         (307, "github-api-failed")])
def test_github_assigned_credential_failure_and_redirects(monkeypatch, status, reason):
    monkeypatch.setenv("TASK_REPOSITORY", "org/repo")
    requests = []
    def transport(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer repo-test-only"
        assert "X-Task-Token" not in request.headers
        return httpx.Response(status, headers={"Location": "https://different.test"}, json={})
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
            github = worker.GitHub(client, "org/repo")
            github.headers["Authorization"] = "Bearer repo-test-only"
            github.expires_at = worker.time.time() + 3600
            await github.call("GET", "/contents")
    with pytest.raises(worker.TaskFailure, match=reason):
        asyncio.run(fetch())
    assert len(requests) == 1


def test_github_credential_rejects_cross_repo_assignment(monkeypatch):
    monkeypatch.setenv("TASK_REPOSITORY", "org/other")
    with pytest.raises(worker.TaskFailure, match="repository-credential-binding-mismatch"):
        worker.GitHub(None, "org/repo")


@pytest.mark.parametrize("url", ["http://acceptor", "https://user:password@acceptor", "https://acceptor?token=value", "https://acceptor/#fragment"])
def test_gateway_url_rejects_unsafe_origins(url):
    with pytest.raises(ValueError, match="https-gateway-origin-required"):
        worker._validate_gateway_url(url)


@pytest.mark.parametrize("name", ["REPO_GITHUB_PAT", "CODEFACTORY_AUTH_TOKEN", "TASK_CALLBACK_TOKEN", "CODER_API_KEY"])
def test_model_input_rejects_all_assigned_secrets(monkeypatch, name):
    monkeypatch.setenv(name, "secret-test-only")
    with pytest.raises(worker.TaskFailure, match="secret-in-model-context"):
        worker._reject_secret_content("patch containing secret-test-only")


def test_report_does_not_forward_credentials_on_redirect(monkeypatch):
    calls = []
    def transport(request):
        calls.append(request)
        assert "Authorization" not in request.headers
        assert request.headers["X-Task-Token"] == "callback-test-secret"
        assert "repo-test-only" not in request.content.decode()
        return httpx.Response(307, headers={"Location": "https://different-host/tasks"}, json={})
    async def report():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
            await worker._report_result(client, {"X-Task-Token": "callback-test-secret"},
                                        "https://acceptor", "task", {"task_id": "task", "status": "failed"})
    with pytest.raises(worker.TaskFailure, match="result-report-failed"):
        asyncio.run(report())
    assert len(calls) == 3 and {request.url.host for request in calls} == {"acceptor"}


@pytest.mark.parametrize("response, expected", [
    (httpx.Response(400, json={"error": {"message": "synthetic context limit exceeded", "type": "invalid_request_error",
                                      "code": "context_limit", "param": "max_tokens"}, "prompt": "do-not-log-prompt"}),
     "synthetic context limit exceeded"),
    (httpx.Response(400, text="synthetic unsupported response_format\njson_object"), "synthetic unsupported response_format json_object"),
    (httpx.Response(400, text=""), "response="),
    (httpx.Response(400, json={"error": {"message": "model-test-secret github-test-secret gateway-test-secret callback-test-secret"}}),
     "[REDACTED]"),
    (httpx.Response(400, text="x" * 2000 + "model-test-secret"), "[truncated]"),
])
def test_coder_http_error_diagnostics_persist_safely(monkeypatch, caplog, response, expected):
    monkeypatch.setenv("CODER_API_KEY", "model-test-secret")
    monkeypatch.setenv("CODER_URL", "https://model/v1")
    monkeypatch.setenv("CODER_MODEL", "configured-coder")
    monkeypatch.setenv("REPO_GITHUB_PAT", "github-test-secret")
    monkeypatch.setenv("CODEFACTORY_AUTH_TOKEN", "gateway-test-secret")
    response.request = httpx.Request("POST", "https://model/v1/chat/completions")
    coder = AsyncMock()
    coder.__aenter__.return_value = coder
    coder.chat.completions.create.side_effect = worker.openai.BadRequestError(
        "model request rejected", response=response, body=None)
    monkeypatch.setattr(worker.openai, "AsyncOpenAI", Mock(return_value=coder))
    with pytest.raises(worker.TaskFailure) as failure:
        asyncio.run(worker._generate_edits({"title": "Fix app"}, {}, {"editable_files": ["src/app.py"]}))
    diagnostic = str(failure.value)
    assert diagnostic.startswith("coder-http-error: status=400 model=configured-coder response=")
    assert expected in diagnostic and diagnostic in caplog.text
    assert len(diagnostic) <= 1012
    for secret in ("model-test-secret", "github-test-secret", "gateway-test-secret", "callback-test-secret", "do-not-log-prompt"):
        assert secret not in diagnostic and secret not in caplog.text
    coder.chat.completions.create.assert_awaited_once()


def test_result_delivery_failure_is_not_success(monkeypatch, tmp_path):
    monkeypatch.setenv("TASK_WORK_DIR", str(tmp_path))
    monkeypatch.delenv("CODER_API_KEY", raising=False)
    client = AsyncMock()
    client.post.return_value = httpx.Response(202, json={"status": "accepted"})
    payload = {"task_id": "2", "files": [{"path": "a.py", "content": "x=1"}]}
    final = asyncio.run(worker._run_worker(client, "https://acceptor", "worker", payload, "2"))
    assert final["error"] == "result-report-failed" and final["report_failed"]
    assert client.post.await_count == 3


@pytest.mark.parametrize("outcome", ["success", "failed", "timeout", "large"])
def test_validation_runner_is_fixed_sandboxed_and_bounded(monkeypatch, outcome):
    process = SimpleNamespace(returncode=None, stdout=SimpleNamespace(read=AsyncMock(side_effect=[b"passed", b""])),
                              wait=AsyncMock(return_value=0), kill=Mock())
    if outcome == "failed":
        process.wait.return_value = 1
    elif outcome == "large":
        process.stdout.read.side_effect = [b"x" * 65537]
    elif outcome == "timeout":
        process.stdout.read.side_effect = asyncio.TimeoutError
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(worker.asyncio, "create_subprocess_exec", spawn)
    policy = {"validation_argv": ["/usr/local/bin/python", "-m", "pytest", "tests/test_small.py"], "validation_timeout": 1}
    if outcome == "success":
        assert asyncio.run(worker._run_validation("/task/workspace", policy)) == "passed"
    else:
        with pytest.raises((worker.TaskFailure, asyncio.TimeoutError)):
            asyncio.run(worker._run_validation("/task/workspace", policy))
    args, kwargs = spawn.call_args
    assert args[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in args and "--clearenv" in args and "--new-session" in args
    assert args[-5:] == ("--", *policy["validation_argv"])
    assert "/var/run/secrets" not in args
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin"}
    if outcome in ("timeout", "large"):
        process.kill.assert_called_once()
