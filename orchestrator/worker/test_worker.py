"""
Unit tests for the worker sidecar (worker.py).

Covers the pure helpers (edit extraction, task-id extraction, patch rendering),
the CodeEditor file writer, and the heuristic self-review fallback.
"""

import asyncio
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest
import worker
from worker import (
    CodeEditor,
    _extract_edits,
    _extract_task_id,
    _failure_result,
    render_patch,
)

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
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "gateway"))
    import server

    payload = {
        "name": "orchestrator-agent", "ref": "refs/heads/main",
        "payload": {
            "issue_number": 2, "issue_title": "Update greeting", "repo": "org/repo",
            "issue_url": "https://github.com/org/repo/issues/2", "default_branch": "main",
            edit_key: [{"path": "src/greeting.py", "content": "greeting = 'hello'\n"}],
        },
    }
    template = {
        "metadata": {"labels": {"app": "worker", "controller-uid": "old"}},
        "spec": {
            "parallelism": 2, "completions": 8, "backoffLimit": 0,
            "template": {
                "metadata": {"labels": {"job-name": "old"}},
                "spec": {"containers": [{"name": "coder", "env": [{
                    "name": "GITHUB_TOKEN", "valueFrom": {"secretKeyRef": {"name": "secrets", "key": "github-token"}},
                }]}]},
            },
        },
    }
    original = copy.deepcopy(template)
    created_jobs = []

    def k8s_request(method, path, body=None):
        if method == "GET":
            assert path.endswith("/jobs/orchestrator-worker")
            return template
        assert method == "POST"
        created_jobs.append(body)
        return {"metadata": {"name": body["metadata"]["name"], "uid": "job-uid"}}

    monkeypatch.setattr(server, "_k8s_request", k8s_request)
    monkeypatch.setattr(server, "ALLOWED_REPOS", {"org/repo"})
    monkeypatch.setattr(server, "TOKEN", "test-token")
    monkeypatch.setattr(server, "WORKER_JOB_TEMPLATE", "orchestrator-worker")
    body = json.dumps(payload).encode()
    handler = server.Handler.__new__(server.Handler)
    handler.headers = {"Authorization": "token test-token", "Content-Length": str(len(body))}
    handler.path = "/api/v1/tasks/spawn"
    handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
    handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
    handler.do_POST()
    handler.send_response.assert_called_once_with(202)
    assert json.loads(handler.wfile.getvalue())["task_id"] == 2
    assert len(created_jobs) == 1
    spec = created_jobs[0]["spec"]
    assert (spec["parallelism"], spec["completions"], spec["backoffLimit"]) == (1, 1, 0)
    assert template == original
    assert "controller-uid" not in created_jobs[0]["metadata"]["labels"]
    assert "job-name" not in spec["template"]["metadata"]["labels"]
    for entry in spec["template"]["spec"]["containers"][0]["env"]:
        if "value" in entry:
            monkeypatch.setenv(entry["name"], entry["value"])
    assert json.loads(worker.os.environ["ASSIGNED_TASK_PAYLOAD"]) == payload

    reports = []

    def acceptor(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v1/tasks/2"
        reports.append(json.loads(request.content))
        return httpx.Response(202, json={"status": "accepted"})

    client_class = httpx.AsyncClient
    clients = []

    def make_client(**kwargs):
        client = client_class(transport=httpx.MockTransport(acceptor), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(worker.httpx, "AsyncClient", make_client)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("TASK_WORK_DIR", str(tmp_path))
    active_before = worker.ACTIVE_JOBS._value.get()
    result = asyncio.run(worker.main_async(acceptor_url="http://acceptor"))
    assert result["task_id"] == "2"
    assert "error" not in result
    assert result["score"] == 0.7
    assert (tmp_path / "src/greeting.py").read_text() == "greeting = 'hello'\n"
    assert reports == [result]
    assert len(clients) == 1 and clients[0].is_closed
    assert worker.ACTIVE_JOBS._value.get() == active_before


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


def test_assigned_issue_only_is_unsupported(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(worker, "CodeEditor", Mock(side_effect=AssertionError("No editor expected")))
    monkeypatch.setattr(worker, "judge_review", AsyncMock(side_effect=AssertionError("No judge expected")))
    payload = {"payload": {"issue_number": 2, "repo": "org/repo", "issue_title": "Fix bug"}}
    result = asyncio.run(worker._run_worker(client, "http://acceptor", "worker", payload, "2"))
    assert result["task_id"] == "2"
    assert result["status"] == "unsupported"
    assert result["score"] == 0.0
    assert "issue-to-code/PR" in result["error"]
    client.get.assert_not_awaited()
    client.post.assert_awaited_once_with(
        "http://acceptor/api/v1/tasks/2", headers={"Authorization": f"token {worker.os.environ.get('GITHUB_TOKEN', '')}",
                                                   "Content-Type": "application/json"}, json=result,
    )


def test_legacy_worker_still_spawns_polls_and_reports(monkeypatch, tmp_path):
    requests = []

    def acceptor(request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/spawn"):
            assert json.loads(request.content) == {"worker_id": "legacy", "payload": {"files": [{"path": "a.py", "content": "x=1"}]}}
            return httpx.Response(202, json={"task_id": 2})
        return httpx.Response(200, json={})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(acceptor), **kwargs))
    monkeypatch.setenv("TASK_WORK_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = asyncio.run(worker.run_worker("http://acceptor", "legacy", {"files": [{"path": "a.py", "content": "x=1"}]}))
    assert result["task_id"] == "2"
    assert requests == [("POST", "/api/v1/tasks/spawn"), ("GET", "/api/v1/tasks/2"), ("POST", "/api/v1/tasks/2")]
    assert (tmp_path / "a.py").read_text() == "x=1"


@pytest.mark.parametrize("failure", [None, "timeout", "sdk-timeout", "error"])
def test_judge_uses_awaited_openai_client_and_closes(monkeypatch, failure):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("JUDGE_URL", "http://judge/v1")
    monkeypatch.setenv("JUDGE_MODEL", "test-judge")
    judge = MagicMock()
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='{"score": 0.9, "review": "Looks good"}'),
    )]))
    if failure == "timeout":
        create.side_effect = httpx.ReadTimeout("timed out")
    elif failure == "sdk-timeout":
        create.side_effect = worker.openai.APITimeoutError(request=httpx.Request("POST", "http://judge/v1"))
    elif failure == "error":
        create.side_effect = RuntimeError("judge unavailable")
    judge.chat.completions.create = create
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=judge)
    context.__aexit__ = AsyncMock(return_value=False)
    factory = Mock(return_value=context)
    monkeypatch.setattr(worker.openai, "AsyncOpenAI", factory)
    result = asyncio.run(worker.judge_review(None, "test patch", "output"))
    factory.assert_called_once_with(api_key="test-key", base_url="http://judge/v1", timeout=worker.HTTP_TIMEOUT, max_retries=0)
    create.assert_awaited_once_with(
        model="test-judge", messages=[
            {"role": "system", "content": "You are a code reviewer for autonomous coding agents."},
            {"role": "user", "content": "Review the following code changes:\n\ntest patch"},
        ], temperature=0.1, max_tokens=512,
    )
    context.__aexit__.assert_awaited_once()
    if failure:
        assert result[0] == 0.0
        assert ("timed out" if "timeout" in failure else "failed") in result[1]
    else:
        assert result == (0.9, "Looks good")


def test_judge_without_key_uses_heuristic(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    factory = Mock(side_effect=AssertionError("No model calls expected"))
    monkeypatch.setattr(worker.openai, "AsyncOpenAI", factory)
    score, review = asyncio.run(worker.judge_review(None, "patch", "output"))
    assert score == 0.7
    assert "No OpenAI API key" in review
    factory.assert_not_called()
