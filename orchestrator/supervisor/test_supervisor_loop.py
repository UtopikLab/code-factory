"""Unit tests for pure helper functions in supervisor_loop."""

from orchestrator.coder_worker.worker import (
    CodeEditor,
    _extract_edits,
    render_patch,
)

from orchestrator.supervisor.supervisor_loop import (
    build_evaluation_prompt,
    make_eval_result,
    make_worker_output,
    passes_acceptance,
)


def test_build_evaluation_prompt_contains_issue_and_worker():
    prompt = build_evaluation_prompt("Fix the crash", "worker-42", "diff-line")
    assert "Fix the crash" in prompt
    assert "worker-42" in prompt
    assert "<diff>" in prompt
    assert "</diff>" in prompt


def test_build_evaluation_prompt_has_scoring_instruction():
    prompt = build_evaluation_prompt("t", "w", "d")
    assert "0.0 to 1.0" in prompt


def test_make_eval_result_structure():
    result = make_eval_result("w1", 0.85, "looks good", {"a": 1})
    assert result["worker_id"] == "w1"
    assert result["score"] == 0.85
    assert result["review"] == "looks good"
    assert result["output"] == {"a": 1}


def test_make_worker_output_merges_extra():
    extra = {"output": "o", "score": 0.9}
    output = make_worker_output("task-1", 3, "success", extra, "2020-01-01T00:00:00")
    assert output["task_id"] == "task-1"
    assert output["worker_id"] == 3
    assert output["status"] == "success"
    assert output["score"] == 0.9
    assert output["timestamp"] == "2020-01-01T00:00:00"


def test_make_worker_output_passthrough():
    output = make_worker_output("t", 0, "timeout", {"error": "boom"}, "ts")
    assert output["error"] == "boom"
    assert output["status"] == "timeout"


def _make_state(score):
    return {
        "task_id": "t",
        "issue_number": 0,
        "issue_title": "",
        "issue_url": "",
        "repo": "r",
        "status": "complete",
        "plan": "",
        "iteration": 0,
        "max_iterations": 0,
        "worker_outputs": [],
        "best_output": {"score": score},
        "error": None,
        "created_at": "now",
    }


def test_passes_acceptance_true():
    assert passes_acceptance(_make_state(0.8)) is True


def test_passes_acceptance_false():
    assert passes_acceptance(_make_state(0.6)) is False


def test_code_editor_insert(tmp_path):
    editor = CodeEditor(str(tmp_path))
    edits = [{"path": "a.txt", "content": "c = 3\n"}]
    output, changed, contents = editor.apply_edits(edits)
    assert changed == ["a.txt"]
    assert contents == [("a.txt", "c = 3\n")]
    assert "c = 3\n" in output


def test_code_editor_replace(tmp_path):
    editor = CodeEditor(str(tmp_path))
    edits = [{"path": "a.txt", "content": "a = 10\n"}]
    _output, changed, contents = editor.apply_edits(edits)
    assert changed == ["a.txt"]
    assert contents[0][1] == "a = 10\n"


def test_code_editor_skips_empty(tmp_path):
    editor = CodeEditor(str(tmp_path))
    _output, changed, contents = editor.apply_edits([{"path": "", "content": "x"}])
    assert changed == []
    assert contents == []


def test_render_patch_simple():
    patch = render_patch(["a.txt"], [("a.txt", "x")])
    assert "diff --worker" in patch
    assert "--- a.txt" in patch
    assert "+++ a.txt" in patch
    assert "new-file-mode +0000" in patch


def test_render_patch_multiline():
    patch = render_patch(["a.txt"], [("a.txt", "l1\nl2")])
    assert "l1\nl2" in patch


def test_extract_edits_no_edits():
    assert _extract_edits({"a": 1}) == []


def test_extract_edits_from_files_key():
    edits = [{"path": "p", "content": "c"}]
    assert _extract_edits({"files": edits}) == edits


def test_extract_edits_from_edits_key():
    edits = [{"path": "p", "content": "c"}]
    assert _extract_edits({"edits": edits}) == edits


def test_extract_edits_from_changes_key():
    edits = [{"path": "p", "content": "c"}]
    assert _extract_edits({"changes": edits}) == edits


def test_extract_edits_non_dict():
    assert _extract_edits("not a dict") == []
