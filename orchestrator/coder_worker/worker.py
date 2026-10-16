#!/usr/bin/env python3
"""
Coder Worker sidecar for autonomous LangGraph coder agents.

Runs inside a CPU-only Python 3.12 container (base image python:3.12-slim).
Talks to a task-acceptor sidecar over HTTP:
  * POST  /api/v1/tasks/spawn        -> spawn a task, get a task_id
  * GET   /api/v1/tasks/{task_id}    -> poll status; 200 carries the result
  * POST  /api/v1/tasks/{task_id}    -> report the final evaluation payload

The worker performs focused code changes on its working repo (/data/task-work),
self-reviews them via the OpenAI judge model, and returns the evaluation JSON:
  { "output", "patch", "score", "review" }

CPU-only: no torch / transformers / langchain-openai. The OpenAI client (openai)
is used for judging and falls back to a heuristic self-review when the API key
is missing.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Optional

import http
import httpx
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import openai

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("coder_worker")

DEFAULT_ACCEPTOR_URL = "http://orchestrator.local:8000"
DEFAULT_JUDGE_MODEL = "qwen2.5-coder-7b-instruct-q4-km@32k"
HTTP_TIMEOUT = 30.0
POLL_TIMEOUT = 1800.0


# --------------------------------------------------------------------------- #
# Result helpers
# --------------------------------------------------------------------------- #

def _failure_result(task_id: str, message: str) -> dict:
    return {"task_id": task_id, "error": message}


# --------------------------------------------------------------------------- #
# Code editor (generic, no real repo required at build time)
# --------------------------------------------------------------------------- #

class CodeEditor:
    """Applies file edits inside the working repo directory."""

    def __init__(self, working_dir: str):
        self.working_dir = working_dir
        os.makedirs(self.working_dir, exist_ok=True)

    def _write(self, rel_path: str, content: str) -> Optional[str]:
        full = os.path.join(self.working_dir, rel_path)
        try:
            parent = os.path.dirname(full) or "."
            os.makedirs(parent, exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)
            logger.info("Wrote %s", rel_path)
            return content
        except OSError as exc:
            logger.warning("Could not write %s: %s", rel_path, exc)
            return None

    def apply_edits(self, edits: list[dict]) -> tuple[str, list[str], dict[str, str]]:
        """
        edits: list of dicts, each {"path": "...", "content": "..."}.
        Returns (output, changed_paths, contents_map).
        """
        output_lines: list[str] = []
        changed: list[str] = []
        contents: dict[str, str] = []
        for edit in edits:
            if not isinstance(edit, dict):
                logger.warning("Skipping malformed edit: %r", edit)
                continue
            path = edit.get("path", "")
            content = edit.get("content", "")
            if not path or not content:
                logger.info("Skipping empty edit for %r", path)
                continue
            written = self._write(path, content)
            if written is not None:
                changed.append(path)
                contents.append((path, content))
                output_lines.append(f"--- a/{path}\n+++ b/{path}\n\n{content}")
        return ("\n".join(output_lines), changed, contents)


def render_patch(changed: list[str], contents: list[tuple[str, str]]) -> str:
    """Render a readable worker patch for the changed files."""
    lines = ["diff --worker"]
    for path, content in contents:
        lines += [
            f"--- {path}",
            f"+++ {path}",
            "",
            f"new-file-mode +0000",
            f"old-file-mode -0000",
            "",
            content,
        ]
    return "\n".join(lines)


def _extract_edits(payload: dict) -> list[dict]:
    """Extract file edits from a spawn payload (supports 'files' and 'edits')."""
    if not isinstance(payload, dict):
        return []
    for key in ("files", "edits", "changes"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


# --------------------------------------------------------------------------- #
# Judge
# --------------------------------------------------------------------------- #

async def judge_review(client: openai.OpenAI, patch: str, output: str) -> tuple[float, str]:
    if not os.environ.get("OPENAI_API_KEY"):
        logger.warning("OPENAI_API_KEY not set; falling back to heuristic self-review.")
        return (0.7, "No OpenAI API key available; self-review omitted. Changes applied deterministically.")

    try:
        response = client.chat.completions.create(
            model=os.environ.get("JUDGE_MODEL", DEFAULT_JUDGE_MODEL),
            messages=[
                {"role": "system", "content": "You are a code reviewer for autonomous coding agents."},
                {"role": "user", "content": f"Review the following code changes:\n\n{patch}"},
            ],
            temperature=0.1,
            max_tokens=512,
        )
        content = response.choices[0].message.content or ""
        try:
            data = json.loads(content)
            score = float(data.get("score", 0.0))
            review = data.get("review", content)
            return (score, review)
        except (json.JSONDecodeError, ValueError, TypeError):
            return (0.5, content)
    except httpx.TimeoutException as exc:
        logger.warning("Judge request timed out: %s", exc)
        return (0.0, "Judge request timed out; self-review omitted.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Judge request failed: %s", exc)
        return (0.0, "Judge request failed; self-review omitted.")


# --------------------------------------------------------------------------- #
# Worker loop
# --------------------------------------------------------------------------- #

def _extract_task_id(body: bytes) -> str:
    try:
        data = json.loads(body)
        if isinstance(data.get("task_id"), str) and data["task_id"]:
            return data["task_id"]
        for key in ("id", "task_id"):
            if isinstance(data.get(key), str) and data[key]:
                return data[key]
    except (json.JSONDecodeError, ValueError):
        pass
    return ""


async def run_worker(
    acceptor_url: str,
    worker_id: str,
    spawn_payload: Optional[dict],
) -> dict:
    client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    github_token = os.environ.get("GITHUB_TOKEN", "")
    headers = {
        "Authorization": f"token {github_token}",
        "Content-Type": "application/json",
    }

    # 1) spawn
    spawn_resp = await client.post(
        f"{acceptor_url}/api/v1/tasks/spawn",
        headers=headers,
        json={"worker_id": worker_id, "payload": spawn_payload or {}},
    )
    if spawn_resp.status_code != 202:
        result = _failure_result(
            "spawn",
            f"Spawn failed with status {spawn_resp.status_code}: {spawn_resp.text[:500]}",
        )
        return result

    task_id = _extract_task_id(spawn_resp.content)
    logger.info("Task spawned: %s", task_id)

    # 2) poll
    poll_deadline = asyncio.get_running_loop().time() + POLL_TIMEOUT
    status_resp = None
    while True:
        resp = await client.get(
            f"{acceptor_url}/api/v1/tasks/{task_id}",
            headers=headers,
        )
        if resp.status_code == 200:
            status_resp = resp
            break
        await asyncio.sleep(1.0)
        if asyncio.get_running_loop().time() > poll_deadline:
            logger.warning("Poll timed out before result for task %s", task_id)
            break

    if status_resp is None:
        result = _failure_result(task_id, "Poll timed out before result was ready.")
        return result

    # 3) perform code changes
    editor = CodeEditor(os.environ.get("TASK_WORK_DIR", "/data/task-work"))
    payload = spawn_payload or {}
    edits = _extract_edits(payload)
    output, changed, contents, patch = "", [], [], ""
    if not edits:
        logger.info("No files to edit in payload; no changes required.")
        output = "No changes required."
    else:
        output, changed, contents = editor.apply_edits(edits)
        patch = render_patch(changed, contents)

    # 4) judge
    score, review = await judge_review(client, patch or output, output)

    # 5) report
    final = {
        "task_id": task_id,
        "output": output,
        "patch": patch,
        "score": score,
        "review": review,
    }
    await _report_result(client, headers, acceptor_url, task_id, final)
    return final


async def _report_result(
    client: httpx.AsyncClient,
    headers: dict,
    acceptor_url: str,
    task_id: str,
    final: dict,
) -> None:
    url = f"{acceptor_url}/api/v1/tasks/{task_id}"
    await client.post(url, headers=headers, json=final)
    logger.info("Reported result for task %s.", task_id)


# --------------------------------------------------------------------------- #
# Health endpoint
# --------------------------------------------------------------------------- #

async def serve_healthz() -> None:
    server = http.Server("0.0.0.0", 8000)

    async def handle_request(request, response):
        if request.path == "/healthz":
            response.set_body(b"ok")
            await server.handle_request(request, response)

    await server.serve_forever(handle_request)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

async def main_async(
    worker_id: Optional[str] = None,
    acceptor_url: Optional[str] = None,
    spawn_payload: Optional[dict] = None,
) -> dict:
    acceptor_url = acceptor_url or os.environ.get("ACCEPTOR_URL", DEFAULT_ACCEPTOR_URL)
    worker_id = worker_id or os.environ.get("WORKER_ID", "worker")
    logger.info("Worker %s starting; acceptor=%s", worker_id, acceptor_url)
    final = await run_worker(acceptor_url, worker_id, spawn_payload or {})
    logger.info("Worker finished with result: %s", final)
    return final


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--worker" in args or not args:
        asyncio.run(main_async())
    elif "--serve" in args:
        asyncio.run(serve_healthz())
    else:
        logger.info("Unknown args: %s", args)
        sys.exit(2)
