"""Candidate integrity, review, publication, and gateway completion."""

import asyncio
import base64
import difflib
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import PurePosixPath


def verified_candidate_patch(task, candidate, github_request):
    repo = task["repo"] or ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise RuntimeError("candidate-repository-invalid")
    base_sha = candidate.get("base_sha", "")
    if not isinstance(base_sha, str) or not re.fullmatch(r"[a-f0-9]{40,64}", base_sha):
        raise RuntimeError("candidate-base-sha-invalid")
    edits = candidate.get("edits")
    changed_files = candidate.get("changed_files")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 8 or not isinstance(changed_files, list):
        raise RuntimeError("candidate-edit-policy-violation")
    paths = [edit.get("path", "") if isinstance(edit, dict) else "" for edit in edits]
    if len(set(paths)) != len(paths) or set(paths) != set(changed_files):
        raise RuntimeError("candidate-edit-set-mismatch")
    current = github_request("GET", repo, "/git/commits/" + base_sha)
    tree = github_request("GET", repo, "/git/trees/" + current["tree"]["sha"] + "?recursive=1")
    if tree.get("truncated"):
        raise RuntimeError("candidate-base-tree-truncated")
    entries = {entry["path"]: entry for entry in tree.get("tree", []) if entry.get("type") == "blob"}
    diffs = []
    total_size = 0
    for edit in edits:
        path = edit.get("path", "")
        parts = path.split("/")
        content = edit.get("content")
        if (not path or PurePosixPath(path).is_absolute() or "\\" in path
                or any(part in ("", ".", "..") or part.startswith(".") for part in parts)
                or any(part.lower() in {"dockerfile", "requirements.txt", "secrets.yaml", "secrets.yml"} for part in parts)
                or not isinstance(content, str) or not content.strip()):
            raise RuntimeError("candidate-edit-policy-violation")
        total_size += len(content.encode())
        if total_size > 524288:
            raise RuntimeError("candidate-edits-too-large")
        entry = entries.get(path)
        if entry and (entry.get("mode") not in ("100644", "100755") or entry.get("size", 0) > 262144):
            raise RuntimeError("candidate-base-file-unsupported")
        original = ""
        if entry:
            blob = github_request("GET", repo, "/git/blobs/" + entry["sha"])
            if blob.get("encoding") != "base64":
                raise RuntimeError("candidate-base-blob-invalid")
            original = base64.b64decode(blob["content"].replace("\n", ""), validate=True).decode("utf-8")
        diffs.append("".join(difflib.unified_diff(original.splitlines(keepends=True), content.splitlines(keepends=True),
                                                  fromfile="a/" + path, tofile="b/" + path)))
    verified = "\n".join(diffs)
    if not verified.strip() or candidate.get("patch") != verified:
        raise RuntimeError("candidate-patch-edits-mismatch")
    return {**candidate, "patch": verified}


async def prepare_judge_model(client, endpoint, model, key, runtime):
    enabled = (runtime or {}).get("judge_native_download", os.environ.get(
        "JUDGE_NATIVE_DOWNLOAD", str(endpoint == "http://llama-server-args-gpu1.inference.svc.cluster.local:8000/v1")).lower() == "true")
    if not enabled:
        return
    url = endpoint.rstrip("/").removesuffix("/v1") + "/models"
    headers = {"Authorization": "Bearer " + key}
    async with asyncio.timeout(900):
        requested = False
        while True:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            entry = next((entry for entry in response.json()["data"]
                          if entry["id"] == model or model in entry.get("aliases", [])), None)
            if entry:
                status = entry.get("status", {})
                if status.get("failed"):
                    raise RuntimeError("native-model-download-or-load-failed")
                if status.get("value") in ("unloaded", "loaded", "sleeping"):
                    return
            if not requested and (entry is None or entry.get("status", {}).get("value") == "incomplete"):
                response = await client.post(url, headers=headers, json={"model": model})
                if response.status_code == 400:
                    detail = response.json().get("error", {}).get("message", "")
                    if "is already downloading" not in detail and "is running, unload it first" not in detail:
                        response.raise_for_status()
                else:
                    response.raise_for_status()
                requested = True
            await asyncio.sleep(1)


async def judge_candidate(task, candidate, runtime, httpx_module):
    endpoint = (runtime or {}).get("judge_url", os.environ.get("JUDGE_URL", "http://llama-server-args-gpu1.inference.svc.cluster.local:8000/v1"))
    model = (runtime or {}).get("judge_model", os.environ.get("JUDGE_MODEL", "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M"))
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("judge-credential-required")
    if candidate.get("validation", {}).get("status") != "passed":
        raise RuntimeError("candidate-validation-not-passed")
    prompt = json.dumps({"task": json.loads(task["payload"]), "candidate": candidate}, sort_keys=True)
    async with httpx_module.AsyncClient(timeout=90) as client:
        await prepare_judge_model(client, endpoint, model, key, runtime)
        response = await client.post(endpoint.rstrip("/") + "/chat/completions",
                                     headers={"Authorization": "Bearer " + key},
                                     json={"model": model, "temperature": 0,
                                           "response_format": {"type": "json_object"},
                                           "messages": [
                                               {"role": "system", "content": 'Evaluate the candidate patch and its validation evidence against the task. Return JSON only: {"score": 0.0, "review": "..."}.'},
                                               {"role": "user", "content": prompt}], "max_tokens": 1024})
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    result = json.loads(content)
    score = float(result["score"])
    if not 0 <= score <= 1:
        raise RuntimeError("judge-score-invalid")
    return score, str(result.get("review", ""))[:4000]


async def plan_task(task, catalog, runtime, max_total_attempts, httpx_module):
    endpoint = (runtime or {}).get("judge_url", os.environ.get("JUDGE_URL", "http://llama-server-args-gpu1.inference.svc.cluster.local:8000/v1"))
    model = (runtime or {}).get("judge_model", os.environ.get("JUDGE_MODEL", "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M"))
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("planner-credential-required")
    request = {"task": json.loads(task["payload"]), "roles": list(catalog["roles"]),
               "max_total_attempts": max_total_attempts}
    async with httpx_module.AsyncClient(timeout=90) as client:
        await prepare_judge_model(client, endpoint, model, key, runtime)
        response = await client.post(endpoint.rstrip("/") + "/chat/completions",
                                     headers={"Authorization": "Bearer " + key},
                                     json={"model": model, "temperature": 0,
                                           "messages": [
                                               {"role": "system", "content": "Plan a bounded implementation for this task. Return concise JSON with a single string field named plan. Do not include credentials or attempt to perform work."},
                                               {"role": "user", "content": json.dumps(request, sort_keys=True)}],
                                           "max_tokens": 1024})
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    result = json.loads(content)
    plan = result.get("plan")
    if not isinstance(plan, str) or not plan.strip() or len(plan) > 8000:
        raise RuntimeError("planner-output-invalid")
    return plan.strip()


def publish_candidate(task, candidate, runtime, github_request):
    repo = task["repo"] or ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise RuntimeError("pr-repository-invalid")
    runtime = runtime or {}
    allowed = {value.strip() for value in runtime.get("allowed_base_branches", os.environ.get("PR_ALLOWED_BASE_BRANCHES", "")).split(",") if value.strip()}
    reviewers = {value.strip() for value in runtime.get("reviewers", os.environ.get("PR_REVIEWERS", "")).split(",") if value.strip()}
    if not allowed or not reviewers:
        raise RuntimeError("pr-policy-required")
    branch = candidate.get("base_branch", "")
    if branch not in allowed or not re.fullmatch(r"[A-Za-z0-9._/-]{1,255}", branch):
        raise RuntimeError("pr-base-branch-not-allowed")
    if any(not re.fullmatch(r"[A-Za-z0-9-]{1,39}", reviewer) for reviewer in reviewers):
        raise RuntimeError("pr-reviewer-policy-invalid")
    edits = candidate.get("edits")
    if not isinstance(edits, list) or not edits or not isinstance(candidate.get("base_sha"), str):
        raise RuntimeError("candidate-publish-data-missing")
    for edit in edits:
        path = edit.get("path", "") if isinstance(edit, dict) else ""
        parts = path.split("/")
        if (not path or PurePosixPath(path).is_absolute() or "\\" in path
                or any(part in ("", ".", "..") or part.startswith(".") for part in parts)
                or any(part.lower() in {".github", "dockerfile", "requirements.txt", "secrets.yaml"} for part in parts)
                or not isinstance(edit.get("content"), str)):
            raise RuntimeError("candidate-edit-policy-violation")
    current = github_request("GET", repo, "/git/ref/heads/" + urllib.parse.quote(branch, safe=""))
    base_sha = candidate["base_sha"]
    if current.get("object", {}).get("sha") != base_sha:
        raise RuntimeError("base-branch-changed")
    commit = github_request("GET", repo, "/git/commits/" + base_sha)
    tree = github_request("POST", repo, "/git/trees", {"base_tree": commit["tree"]["sha"], "tree": [
        {"path": edit["path"], "mode": "100644", "type": "blob", "content": edit["content"]}
        for edit in edits]})
    task_id = task["task_id"]
    work_branch = f"code-factory/{task_id[:48]}"
    ref_path = "/git/ref/heads/" + urllib.parse.quote(work_branch, safe="")
    existing_ref = github_request("GET", repo, ref_path)
    if existing_ref:
        existing_commit = github_request("GET", repo, "/git/commits/" + existing_ref["object"]["sha"])
        if existing_commit.get("tree", {}).get("sha") != tree["sha"]:
            raise RuntimeError("existing-task-branch-conflict")
    else:
        created = github_request("POST", repo, "/git/commits", {
            "message": f"Implement issue #{task['issue']}", "tree": tree["sha"], "parents": [base_sha]})
        github_request("POST", repo, "/git/refs", {"ref": "refs/heads/" + work_branch, "sha": created["sha"]})
    pull_path = "/pulls?state=all&head=" + urllib.parse.quote(repo.split("/")[0] + ":" + work_branch, safe="")
    pulls = github_request("GET", repo, pull_path) or []
    if pulls:
        pull = pulls[0]
        if pull.get("state") != "open" or pull.get("base", {}).get("ref") != branch:
            raise RuntimeError("existing-pr-policy-conflict")
    else:
        pull = github_request("POST", repo, "/pulls", {
            "title": f"Implement issue #{task['issue']}", "head": work_branch, "base": branch,
            "body": f"Closes issue #{task['issue']}\n\nValidated candidate selected by Code Factory. Human review required. Task: {task_id}"})
    number = pull.get("number")
    if not isinstance(number, int) or number <= 0:
        raise RuntimeError("pr-creation-unverified")
    github_request("POST", repo, f"/pulls/{number}/requested_reviewers", {"reviewers": sorted(reviewers)})
    url = pull.get("html_url", "")
    if not re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/{number}", url, re.IGNORECASE):
        raise RuntimeError("pr-identity-invalid")
    return url


def complete_via_gateway(task_id, status, runtime, details, urlopen):
    token = os.environ.get("SUPERVISOR_AUTH_TOKEN", "")
    if not token:
        raise RuntimeError("supervisor-gateway-credential-required")
    body = json.dumps({"task_id": task_id, "status": status, **details}).encode()
    acceptor_url = (runtime or {}).get("acceptor_url", os.environ.get("ACCEPTOR_URL", "http://orchestrator-gateway:8000"))
    request = urllib.request.Request(
        acceptor_url.rstrip("/") + f"/api/v1/supervisor/tasks/{task_id}", data=body,
        headers={"Authorization": "token " + token, "Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}")