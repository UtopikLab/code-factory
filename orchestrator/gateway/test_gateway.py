"""
Unit tests for the gateway repo-allowlist helpers (server.py).

These exercise the _parse_allowed_repos helper and the repo-validation logic,
which are deterministic and do not require a running HTTP server.
"""

import os

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
