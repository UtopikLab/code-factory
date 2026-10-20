"""
Unit tests for the worker sidecar (worker.py).

Covers the pure helpers (edit extraction, task-id extraction, patch rendering),
the CodeEditor file writer, and the heuristic self-review fallback.
"""

import json

import pytest
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
