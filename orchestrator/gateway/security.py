"""Credential, repository identity, and task payload validation."""

import json
import re
import urllib.error
from urllib.parse import quote


class CredentialError(Exception):
    pass


def reject_credentials(payload, *tokens, global_token=""):
    serialized = json.dumps(payload)
    if (any(token and token in serialized for token in (*tokens, global_token))
            or re.search(r"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{16,}", serialized)):
        raise ValueError("credential-in-payload")

    def inspect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if re.search(r"token|secret|password|authorization|credential", key, re.IGNORECASE):
                    raise ValueError("credential-in-payload")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(payload)


def validate_repository(repo, issue, token, github, global_token=""):
    if not repo or not token or len(token) > 4096 or any(char.isspace() for char in token) or token == global_token:
        raise CredentialError("repository-credential-required")
    try:
        metadata = github("GET", "/repos/" + quote(repo, safe="/"), token=token)
        if metadata.get("full_name", "").lower() != repo.lower():
            raise CredentialError("repository-identity-mismatch")
        permissions = metadata.get("permissions", {})
        if permissions.get("pull") is not True or permissions.get("push") is True or metadata.get("archived"):
            raise CredentialError("repository-read-access-unverified")
        for path in ("/contents", "/pulls?per_page=1", "/issues?per_page=1"):
            github("GET", "/repos/" + quote(repo, safe="/") + path, token=token)
        if issue:
            remote = github("GET", f"/repos/{quote(repo, safe='/')}/issues/{issue}", token=token)
            if remote.get("number") != issue or "pull_request" in remote or remote.get("html_url", "").lower() != f"https://github.com/{repo}/issues/{issue}".lower():
                raise CredentialError("repository-issue-mismatch")
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 404):
            raise CredentialError("repository-credential-denied") from None
        raise CredentialError("repository-validation-unavailable") from None
    except (OSError, ValueError, TypeError, AttributeError):
        raise CredentialError("repository-validation-unavailable") from None


def identity(payload):
    nested = payload.get("payload", {})
    repo = payload.get("repo") or nested.get("repo")
    if any(value is not None and not isinstance(value, str) for value in (payload.get("repo"), nested.get("repo"))):
        raise ValueError("invalid-repository")
    if payload.get("repo") and nested.get("repo") and payload["repo"].lower() != nested["repo"].lower():
        raise ValueError("repository-mismatch")
    issue = nested.get("issue_number", payload.get("issue_number"))
    if repo is not None and (not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
                             or any(part in (".", "..") for part in repo.split("/"))):
        raise ValueError("invalid-repository")
    if ("issue_number" in payload or "issue_number" in nested) and (type(issue) is not int or issue <= 0 or not repo):
        raise ValueError("invalid-issue")
    if "issue_number" in nested and "issue_number" in payload and nested["issue_number"] != payload["issue_number"]:
        raise ValueError("issue-mismatch")
    return repo, issue


def is_external_workflow_payload(payload):
    """True when payload appears to originate from the external drain workflow."""
    if not isinstance(payload, dict) or "worker_id" in payload:
        return False
    nested = payload.get("payload")
    return "name" in payload and "ref" in payload and isinstance(nested, dict) and "issue_number" in nested


def validate_assigned_task_id(task_id):
    if type(task_id) is int and task_id >= 0:
        task_id = str(task_id)
    if (
        not isinstance(task_id, str)
        or task_id == "unknown"
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?", task_id)
    ):
        raise ValueError("invalid-assigned-task-id")
    return task_id