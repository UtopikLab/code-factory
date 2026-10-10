#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Supervisor HTTP task-acceptor (sidecar).
# ---------------------------------------------------------------------------
#
# Runs as a sidecar container in the always-on `orchestrator` supervisor pod
# (see orchestrator/supervisor.yaml). It exposes the ONLY HTTP surface of the
# supervisor: `POST /api/v1/tasks/spawn`.
#
# Contract (shared with .github/workflows/drain.yaml):
#
#   POST /api/v1/tasks/spawn
#   Authorization: token <github-token>      # the workflow-scoped PAT
#   Content-Type: application/json
#   Body: {
#     "name": "orchestrator-agent",
#     "ref": "refs/heads/heads-20260101-120000",
#     "payload": {
#       "issue_number": 42,
#       "issue_title": "Fix the thing",
#       "issue_url": "https://github.com/UtopikLab/llm-rig/issues/42",
#       "repo": "UtopikLab/llm-rig",
#       "default_branch": "main"
#     }
#   }
#
# On success the handler:
#   1. validates the token,
#   2. renders orchestrator/agent-job-template.yaml with the per-task values,
#      and creates the `orchestrator-agent` Job,
#   3. blocks (kubectl wait) until the Job completes,
#   4. deletes the Job (its pod is cleaned up with it — backoffLimit 0 means
#      the Job itself is already gone, so this just reaps the pod),
#   5. exits, releasing port 8000 for the next task.
#
# The supervisor's LangGraph loop process shares this pod but is independent:
# it stays resident and waiting; this sidecar owns the Job lifecycle and is
# re-entrant (it rebinds the port on every task).
#
# Resource cost: one lightweight Python process that blocks on a task. The
# supervisor's CPU/RAM `limits` cover it — no extra always-on load.
# ---------------------------------------------------------------------------
import argparse
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _run(cmd, timeout=120):
    """Run a kubectl command, return (returncode, stdout)."""
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


class TaskAcceptanceError(Exception):
    pass


class TaskAccepter:
    def __init__(self, github_token, kubectl_bin="kubectl"):
        self.github_token = github_token
        self.kubectl_bin = kubectl_bin

    def auth_ok(self, auth_header):
        # Accept `token <value>` or a bare `<value>`.
        token = auth_header.strip()
        if token.lower().startswith("token "):
            token = token[6:].strip()
        return bool(token) and token == self.github_token

    def spawn(self, body):
        if not isinstance(body, dict):
            raise TaskAcceptanceError("payload must be a JSON object")
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise TaskAcceptanceError("payload must be a JSON object")

        # Security: the workflow is scoped to a single repo; refuse any task
        # that names a different repository.
        expected_repo = payload.get("repo")
        if not expected_repo or "/" not in expected_repo:
            raise TaskAcceptanceError("payload.repo must be an owner/name slug")

        task_id = f"{expected_repo}#{payload.get('issue_number')}"
        issue_number = payload.get("issue_number")

        # Render the Job template with per-task values.
        template_path = os.environ.get("AGENT_JOB_TEMPLATE", "orchestrator/agent-job-template.yaml")
        with open(template_path) as fh:
            tmpl = fh.read()

        rendered = tmpl.replace("${TASK_ID}", task_id)
        rendered = rendered.replace("${ISSUE_URL}", payload.get("issue_url", ""))
        rendered = rendered.replace("${REPO}", expected_repo)
        rendered = rendered.replace("${GITHUB_TOKEN}", self.github_token)

        # Create the Job.
        rc, out, err = _run([self.kubectl_bin, "apply", "-f", "-"], input=rendered)
        if rc != 0:
            raise TaskAcceptanceError(f"kubectl apply failed: {err.strip()}")

        # Job UID — used by the workflow to poll / dedupe.
        rc, out, err = _run([
            self.kubectl_bin, "get", "job", "orchestrator-agent",
            "-o", "jsonpath={.metadata.uid}", "-n", "orchestrator",
        ])
        job_uid = out.strip() or "unknown"

        # Block until the Job completes (succeed, fail, or abort).
        rc, out, err = _run([
            self.kubectl_bin, "wait", "--for=jobcomplete",
            "job/orchestrator-agent", "-n", "orchestrator", "--timeout=1800s",
        ])
        if rc != 0:
            raise TaskAcceptanceError(f"job did not complete: {err.strip()}")

        # Reap the Job (cleans up the pod). backoffLimit 0 means the Job
        # object is already deleted, so this just removes the pod.
        _run([self.kubectl_bin, "delete", "job", "orchestrator-agent", "-n", "orchestrator"])

        return {
            "uid": job_uid,
            "task_id": task_id,
            "issue_number": issue_number,
            "status": "completed",
        }


def main():
    ap = argparse.ArgumentParser(description="Supervisor HTTP task-acceptor")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--kubectl", default=os.environ.get("KUBECTL_BIN", "kubectl"))
    args = ap.parse_args()

    acceptor = TaskAccepter(
        github_token=os.environ["ACCEPTOR_GITHUB_TOKEN"],
        kubectl_bin=args.kubectl,
    )

    handler = BaseHTTPRequestHandler
    handler.log_message = lambda *a, **k: None  # quiet by default

    class H(handler):
        def do_POST(self):
            if self.path.rstrip("/") != "/api/v1/tasks/spawn":
                self.send_error(404, "not found")
                return

            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400, "invalid JSON body")
                return

            auth = self.headers.get("Authorization", "")
            if not acceptor.auth_ok(auth):
                self.send_error(401, "invalid token")
                return

            try:
                result = acceptor.spawn(body)
            except TaskAcceptanceError as exc:
                self.send_error(400, str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                self.send_error(500, str(exc))
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))

        def do_GET(self):
            # A tiny liveness probe target so the Service health check can
            # confirm the sidecar is alive (the supervisor's exec probes cover
            # the loop process).
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer((args.host, args.port), H)
    print(f"task-acceptor listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
