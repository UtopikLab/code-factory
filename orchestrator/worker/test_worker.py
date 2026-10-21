"""
Unit tests for the worker sidecar (worker.py).

Covers the pure helpers (edit extraction, task-id extraction, patch rendering),
the CodeEditor file writer, and the heuristic self-review fallback.
"""

import asyncio
import base64
import io
import json
from pathlib import Path
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
    monkeypatch.setenv("CODEFACTORY_AUTH_TOKEN", "gateway-test-secret")
    monkeypatch.setenv("TASK_CALLBACK_TOKEN", "callback-test-secret")
    monkeypatch.delenv("CODER_NATIVE_DOWNLOAD", raising=False)
    monkeypatch.delenv("JUDGE_NATIVE_DOWNLOAD", raising=False)


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
    with pytest.raises(worker.TaskFailure, match="repository-credential-required"):
        worker.GitHub(None, "org/repo")


@pytest.mark.parametrize("status,expired,reason", [(401, False, "repository-credential-denied"),
                                                 (403, False, "repository-credential-denied"),
                                                 (307, False, "github-api-failed"),
                                                 (200, True, "repository-credential-expired")])
def test_github_assigned_credential_failure_and_redirects(monkeypatch, status, expired, reason):
    monkeypatch.setenv("REPO_GITHUB_PAT", "repo-test-only")
    monkeypatch.setenv("TASK_REPOSITORY", "org/repo")
    monkeypatch.setenv("TASK_CREDENTIAL_EXPIRES_AT", "1" if expired else "9999999999")
    requests = []
    def transport(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer repo-test-only"
        assert "X-Task-Token" not in request.headers
        return httpx.Response(status, headers={"Location": "https://different.test"}, json={})
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
            await worker.GitHub(client, "org/repo").call("GET", "/contents")
    with pytest.raises(worker.TaskFailure, match=reason):
        asyncio.run(fetch())
    assert len(requests) == (0 if expired else 1)


def test_github_credential_rejects_cross_repo_assignment(monkeypatch):
    monkeypatch.setenv("REPO_GITHUB_PAT", "repo-test-only")
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


@pytest.fixture
def issue_pipeline(monkeypatch, tmp_path):
    pytest.skip("PR publication and lifecycle integration are supervisor-owned")
    monkeypatch.setenv("GATEWAY_TRANSPORT_MODE", "edge-tls")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "gateway"))
    import server

    monkeypatch.setenv("REPO_GITHUB_PAT", "github-test-secret")
    monkeypatch.setenv("TASK_REPOSITORY", "org/repo")
    monkeypatch.setenv("TASK_CREDENTIAL_EXPIRES_AT", "9999999999")
    monkeypatch.setenv("ALLOW_LOCAL_HTTP", "true")
    monkeypatch.setenv("CODER_API_KEY", "model-test-secret")
    monkeypatch.setenv("CODER_URL", "http://model/v1")
    monkeypatch.setenv("CODER_MODEL", "configured-coder")
    monkeypatch.setenv("JUDGE_URL", "http://model/v1")
    monkeypatch.setenv("JUDGE_MODEL", "configured-judge")
    monkeypatch.setenv("TASK_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "state" / "tasks.sqlite3"))
    policy = {"context_files": ["src/greeting.py"], "editable_files": ["src/greeting.py"],
              "validation_argv": ["/usr/local/bin/python", "-m", "pytest", "tests/test_greeting.py"],
              "validation_timeout": 15}
    monkeypatch.setenv("TASK_REPO_POLICIES", json.dumps({"org/repo": policy}))
    monkeypatch.setattr(server, "TOKEN", "gateway-test-secret")
    state = SimpleNamespace(jobs=[], api_calls=[], model_calls=[], native_calls=[], native_downloads={}, labels={"queued"}, comments=[],
                            branches={}, pulls=[], edits=[{"path": "src/greeting.py", "content": "greeting = 'hello'\n"}],
                            score=0.95, model_error=False, fail_lifecycle=False, invalid_identity=False,
                            tree_mode="100644", base_sha="base", policy=policy, malformed_api=False, empty_choices=False,
                            context={"src/greeting.py": "greeting = 'hi'\n"})
    stored = {}
    def kubernetes(method, path, body=None):
        name = path.rsplit("/", 1)[-1]
        if method == "POST":
            stored[body["metadata"]["name"]] = body
            return body
        if name not in stored:
            raise server.KubernetesError(404)
        return stored.pop(name) if method == "DELETE" else stored[name]
    monkeypatch.setattr(server, "_k8s_request", kubernetes)
    monkeypatch.setattr(server.secrets, "token_urlsafe", lambda length: "callback-test-secret")

    def gateway(method, path, body=None):
        raw = json.dumps(body).encode()
        handler = server.Handler.__new__(server.Handler)
        handler.command = method
        handler.headers = {"Authorization": "token gateway-test-secret", "Content-Length": str(len(raw)),
                   "X-Repository-Token": "github-test-secret", "X-Task-Token": "callback-test-secret"}
        handler.client_address = ("127.0.0.1", 1234)
        handler.path = path
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        getattr(handler, f"do_{method}")()
        return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())

    state.gateway = gateway

    def create_job(namespace, template_name, task_id, payload):
        template = {"spec": {"template": {"spec": {"containers": [{"name": "coder"}]}}}}
        job = server._clone_job_from_template(template, namespace, "assigned-worker", task_id, payload)
        state.jobs.append(job)
        return {"name": "assigned-worker", "uid": "job-uid"}

    monkeypatch.setattr(server, "_create_worker_job", create_job)

    def api(method, path, body=None, *, token="github-test-secret"):
        assert token == "github-test-secret"
        state.api_calls.append((method, path, body))
        suffix = path.removeprefix("/repos/org/repo")
        if suffix == "":
            if state.malformed_api:
                return []
            return {"full_name": "org/repo", "default_branch": "release/stable", "permissions": {"push": True}}
        if suffix in ("/contents", "/issues?per_page=1"):
            return []
        if suffix == "/issues/2":
            return {"number": 2, "html_url": "https://github.com/org/other/issues/2" if state.invalid_identity else "https://github.com/org/repo/issues/2",
                    "state": "open", "title": "Update greeting", "body": "Return hello instead of hi."}
        if suffix.startswith("/git/ref/heads/release"):
            return {"object": {"sha": state.base_sha}}
        if suffix == "/git/commits/base":
            return {"tree": {"sha": "base-tree"}}
        if suffix.startswith("/git/trees/base?"):
            return {"tree": [{"path": "src/greeting.py", "mode": state.tree_mode, "type": "blob", "sha": "blob", "size": 16}]}
        if suffix == "/git/blobs/blob":
            return {"encoding": "base64", "content": base64.b64encode(b"greeting = 'hi'\n").decode()}
        if method == "POST" and suffix == "/git/trees":
            assert body["tree"][0]["content"] == state.edits[0]["content"]
            return {"sha": "edited-tree"}
        if method == "POST" and suffix == "/git/commits":
            return {"sha": "edited-commit"}
        if method == "POST" and suffix == "/git/refs":
            state.branches[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return {"object": {"sha": body["sha"]}}
        if suffix.startswith("/git/ref/heads/code-factory/"):
            sha = state.branches.get(suffix.removeprefix("/git/ref/heads/"))
            return {"object": {"sha": sha}} if sha else None
        if suffix == "/git/commits/edited-commit":
            return {"tree": {"sha": "edited-tree"}}
        if method == "GET" and suffix.startswith("/pulls?"):
            return state.pulls
        if method == "POST" and suffix == "/pulls":
            state.pulls.append({"html_url": "https://github.com/org/repo/pull/3", "state": "open",
                                "head": {"ref": body["head"]}, "base": {"ref": body["base"]}})
            assert body["base"] == "release/stable" and "Closes #2" in body["body"]
            return state.pulls[-1]
        if "/issues/2/labels" in suffix:
            if state.fail_lifecycle:
                raise RuntimeError("GitHub unavailable")
            if method == "GET":
                return [{"name": label} for label in state.labels]
            if method == "DELETE":
                state.labels.discard(suffix.rsplit("/", 1)[1])
            if method == "POST":
                state.labels.update(body["labels"])
            return {}
        if "/issues/2/comments" in suffix:
            if method == "GET":
                return state.comments
            state.comments.append(body)
            return body
        raise AssertionError(f"Unexpected mocked API call: {method} {path}")

    monkeypatch.setattr(server, "_github", api)

    def transport(request):
        if request.url.host == "acceptor":
            assert request.headers["Authorization"] == "token gateway-test-secret"
            assert request.headers["X-Task-Token"] == "callback-test-secret"
            assert "github-test-secret" not in request.content.decode()
            code, body = gateway(request.method, request.url.raw_path.decode(), json.loads(request.content))
            return httpx.Response(code, json=body)
        if request.url.host == "api.github.com":
            assert request.headers["Authorization"] == "Bearer github-test-secret"
            data = api(request.method, request.url.path + ("?" + request.url.query.decode() if request.url.query else ""),
                       json.loads(request.content) if request.content else None)
            return httpx.Response(404 if data is None else 200, json=data)
        assert request.url.host == "model"
        if request.url.path == "/models":
            state.native_calls.append((request.method, request.url.path))
            if request.method == "POST":
                model = json.loads(request.content)["model"]
                assert ":" in model and "/" in model
                state.native_downloads[model] = True
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"data": [
                {"id": model, "status": {"value": "unloaded"}} for model in state.native_downloads
            ]})
        body = json.loads(request.content)
        state.model_calls.append(body)
        if state.native_calls:
            assert body["model"] in state.native_downloads
        assert "github-test-secret" not in request.content.decode() and "model-test-secret" not in request.content.decode()
        if state.model_error:
            if isinstance(state.model_error, httpx.Response):
                return state.model_error
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        if state.empty_choices:
            return httpx.Response(200, json={"id": "response", "object": "chat.completion", "created": 1,
                                           "model": body["model"], "choices": []})
        if body["model"] == worker.os.environ["CODER_MODEL"]:
            prompt = json.loads(body["messages"][1]["content"])
            assert prompt["files"] == state.context
            content = json.dumps({"edits": state.edits})
        else:
            assert body["model"] == worker.os.environ["JUDGE_MODEL"]
            assert "Return hello instead of hi" in body["messages"][1]["content"]
            content = json.dumps({"score": state.score, "review": "Scoped change accepted"})
        return httpx.Response(200, json={"id": "response", "object": "chat.completion", "created": 1,
                                       "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop",
                                       "message": {"role": "assistant", "content": content}}]})

    client_class = httpx.AsyncClient
    mock_transport = httpx.MockTransport(transport)
    class OfflineClient(client_class):
        def __init__(self, **kwargs):
            super().__init__(transport=mock_transport, **kwargs)
    monkeypatch.setattr(worker.httpx, "AsyncClient", OfflineClient)
    sdk_class = worker.openai.AsyncOpenAI
    monkeypatch.setattr(worker.openai, "AsyncOpenAI", lambda **kwargs: sdk_class(
        http_client=OfflineClient(), **kwargs))

    async def validate(workspace, config):
        assert Path(workspace).is_relative_to(tmp_path / "work")
        assert Path(workspace, "src/greeting.py").read_text() == "greeting = 'hello'\n"
        assert config["validation_argv"] == state.policy["validation_argv"]
        return "1 passed"
    state.runner = AsyncMock(side_effect=validate)
    monkeypatch.setattr(worker, "_run_validation", state.runner)
    payload = {"name": "drain", "ref": "refs/heads/main", "payload": {"repo": "org/repo", "issue_number": 2}}
    code, spawned = gateway("POST", "/api/v1/tasks/spawn", payload)
    assert code == 202 and len(state.jobs) == 1
    env = state.jobs[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    for entry in env:
        if "value" in entry:
            monkeypatch.setenv(entry["name"], entry["value"])
        else:
            reference = entry["valueFrom"]["secretKeyRef"]
            monkeypatch.setenv(entry["name"], base64.b64decode(stored[reference["name"]]["data"][reference["key"]]).decode())
    state.task_id = spawned["task_id"]
    state.task_path = "/api/v1/tasks/" + state.task_id
    return state


def test_issue_to_pr_without_repository_configuration(issue_pipeline, monkeypatch):
    state = issue_pipeline
    monkeypatch.setenv("TASK_REPO_POLICIES", "{}")
    snapshot = worker._snapshot

    async def snapshot_with_tests(github, editor, sha):
        await snapshot(github, editor, sha)
        full = Path(editor.working_dir, "tests/test_greeting.py")
        full.parent.mkdir()
        full.write_text("def test_greeting(): assert True\n")
        state.context["tests/test_greeting.py"] = full.read_text()
        state.policy = worker._default_repo_policy(editor.working_dir)

    monkeypatch.setattr(worker, "_snapshot", snapshot_with_tests)
    final = asyncio.run(worker.main_async(acceptor_url="https://acceptor"))
    assert final["status"] == "completed", final
    assert final["pr_url"] == "https://github.com/org/repo/pull/3"
    state.runner.assert_awaited_once()
    assert state.gateway("GET", state.task_path)[1]["status"] == "completed"


def test_issue_to_pr_terminal_end_to_end(issue_pipeline, tmp_path):
    state = issue_pipeline
    final = asyncio.run(worker.main_async(acceptor_url="https://acceptor"))
    assert final["status"] == "completed" and "error" not in final, (final, state.api_calls, state.model_calls)
    assert final["pr_url"] == "https://github.com/org/repo/pull/3"
    assert len(state.jobs) == len(state.branches) == len(state.pulls) == 1
    assert [body["model"] for body in state.model_calls] == ["configured-coder", "configured-judge"]
    assert state.runner.await_count == 1
    assert state.labels == {"done"}
    assert len(state.comments) == 1 and final["pr_url"] in state.comments[0]["body"]
    code, saved = state.gateway("GET", state.task_path)
    assert code == 200 and saved["status"] == "completed" and saved["result"] == final and saved["lifecycle"] == "synced"
    assert not list((tmp_path / "work").iterdir())
    assert state.gateway("POST", state.task_path, final)[0] == 200
    assert len(state.comments) == 1
    repeated = asyncio.run(worker.main_async(acceptor_url="https://acceptor"))
    assert repeated == final
    assert len(state.branches) == len(state.pulls) == 1
    assert len([call for call in state.api_calls if call[:2] == ("POST", "/repos/org/repo/pulls")]) == 1


def test_issue_to_pr_downloads_requester_selected_models_before_chat(issue_pipeline, monkeypatch):
    state = issue_pipeline
    coder = "owner/new-coder-GGUF:Q4_K_M"
    judge = "owner/new-judge-GGUF:Q8_0"
    monkeypatch.setenv("CODER_MODEL", coder)
    monkeypatch.setenv("JUDGE_MODEL", judge)
    monkeypatch.setenv("CODER_NATIVE_DOWNLOAD", "true")
    monkeypatch.setenv("JUDGE_NATIVE_DOWNLOAD", "true")
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())
    final = asyncio.run(worker.main_async(acceptor_url="https://acceptor"))
    assert final["status"] == "completed", final
    assert [body["model"] for body in state.model_calls] == [coder, judge]
    assert set(state.native_downloads) == {coder, judge}
    assert state.native_calls == [("GET", "/models"), ("POST", "/models"), ("GET", "/models")] * 2


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
def test_coder_http_error_diagnostics_persist_safely(issue_pipeline, caplog, response, expected):
    state = issue_pipeline
    state.model_error = response

    final = asyncio.run(worker.main_async(acceptor_url="https://acceptor"))

    assert final["status"] == "failed"
    assert not final.get("report_failed")
    diagnostic = final["error"]
    assert diagnostic.startswith("coder-http-error: status=400 model=configured-coder response=")
    assert expected in diagnostic and diagnostic in caplog.text
    assert len(diagnostic) <= 1012
    for secret in ("model-test-secret", "github-test-secret", "gateway-test-secret", "callback-test-secret", "do-not-log-prompt"):
        assert secret not in diagnostic and secret not in caplog.text
    assert state.gateway("GET", state.task_path)[1]["result"]["error"] == diagnostic
    assert len(state.model_calls) == 1
    state.runner.assert_not_awaited()
    assert not state.branches and not state.pulls


def test_failed_issue_retry_publishes_without_overwriting_history(issue_pipeline, monkeypatch):
    import server

    state = issue_pipeline
    state.model_error = True
    failed = asyncio.run(worker.main_async(acceptor_url="https://acceptor"))
    assert failed["status"] == "failed" and not state.pulls
    previous = state.gateway("GET", state.task_path)[1]
    assert previous["lifecycle"] == "synced" and previous["credential_cleanup"] == "deleted"
    code, retried = state.gateway("POST", state.task_path + "/retry", {"retry_key": "operator-1"})
    assert code == 202 and retried["task_id"] != state.task_id
    assert len(state.jobs) == 2
    env = state.jobs[-1]["spec"]["template"]["spec"]["containers"][0]["env"]
    for entry in env:
        if "value" in entry:
            monkeypatch.setenv(entry["name"], entry["value"])
        else:
            reference = entry["valueFrom"]["secretKeyRef"]
            secret = server._k8s_request("GET", server._secret_path(reference["name"]))
            monkeypatch.setenv(entry["name"], base64.b64decode(secret["data"][reference["key"]]).decode())
    state.model_error = False
    completed = asyncio.run(worker.main_async(acceptor_url="https://acceptor"))
    assert completed["status"] == "completed" and completed["pr_url"]
    assert state.gateway("GET", state.task_path)[1]["result"] == previous["result"]
    assert state.gateway("GET", "/api/v1/tasks/" + retried["task_id"])[1]["retry_of"] == state.task_id
    assert len(state.comments) == 2 and len(state.pulls) == 1 and state.labels == {"done"}


@pytest.mark.parametrize("failure", ["generation", "empty", "noop", "whitespace", "traversal", "protected", "validation", "judge", "nan-score", "identity", "symlink", "policy", "api-shape", "empty-choices"])
def test_issue_failures_never_publish(issue_pipeline, monkeypatch, failure):
    state = issue_pipeline
    if failure == "generation":
        state.model_error = True
    elif failure == "empty":
        state.edits = []
    elif failure == "noop":
        state.edits[0]["content"] = "greeting = 'hi'\n"
    elif failure == "whitespace":
        state.edits[0]["content"] = "  \n"
    elif failure in ("traversal", "protected"):
        state.edits[0]["path"] = "../escape" if failure == "traversal" else ".github/workflows/run.yaml"
    elif failure == "validation":
        state.runner.side_effect = worker.TaskFailure("validation-failed")
    elif failure in ("judge", "nan-score"):
        state.score = 0.2 if failure == "judge" else float("nan")
    elif failure == "identity":
        state.invalid_identity = True
    elif failure == "symlink":
        state.tree_mode = "120000"
    elif failure == "policy":
        monkeypatch.setenv("TASK_REPO_POLICIES", "{}")
    elif failure == "api-shape":
        state.malformed_api = True
    elif failure == "empty-choices":
        state.empty_choices = True
    final = asyncio.run(worker.main_async(acceptor_url="https://acceptor"))
    assert final["status"] == "failed" and final["error"]
    assert not state.branches and not state.pulls
    assert not any(method == "POST" and ("/git/" in path or "/pulls" in path) for method, path, _ in state.api_calls)
    assert "done" not in state.labels
    saved = state.gateway("GET", state.task_path)[1]
    assert saved["status"] == "failed" and saved["result"] == {key: value for key, value in final.items() if key != "lifecycle_pending"}


def test_issue_lifecycle_retry_after_success(issue_pipeline):
    state = issue_pipeline
    state.fail_lifecycle = True
    final = asyncio.run(worker.main_async(acceptor_url="https://acceptor"))
    assert final["status"] == "completed" and final["lifecycle_pending"], (final, state.api_calls, state.model_calls)
    saved = state.gateway("GET", state.task_path)[1]
    assert saved["lifecycle"] == "pending" and saved["result"]["pr_url"] == final["pr_url"]
    assert state.labels == {"queued"}
    state.fail_lifecycle = False
    assert state.gateway("POST", state.task_path, saved["result"])[1]["lifecycle"] == "synced"
    assert state.labels == {"done"} and len(state.comments) == 1


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
