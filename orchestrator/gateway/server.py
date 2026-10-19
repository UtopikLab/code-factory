import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("GITHUB_TOKEN", "")


def _parse_allowed_repos(raw):
    """Return None for 'any repo', else a set of allowed owner/name repositories.

    An empty or whitespace-only value means 'allow all' (None), matching the
    wildcard default so that leaving the variable unset also opens the gateway.
    """
    raw = (raw or "").strip()
    if raw == "" or raw == "*":
        return None
    return {r for r in (x.strip() for x in raw.split(",")) if r}


# ALLOWED_REPOS controls which repositories may push tasks to the orchestrator.
#   - "*"            -> accept tasks from ANY repository (no validation)
#   - "a/b,c/d"      -> whitelist of specific owner/name repositories
#   - "a/b"          -> whitelist of a single repository
# The legacy "ALLOWED_REPO" variable is accepted for backward compatibility.
ALLOWED_REPOS_RAW = os.environ.get(
    "ALLOWED_REPOS", os.environ.get("ALLOWED_REPO", "*")
)
ALLOWED_REPOS = _parse_allowed_repos(ALLOWED_REPOS_RAW)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        if auth != f"token {TOKEN}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"invalid-json"}')
            return

        repo = payload.get("repo") or payload.get("payload", {}).get("repo")
        if repo and ALLOWED_REPOS is not None and repo not in ALLOWED_REPOS:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error":"repo-not-allowed"}')
            return

        response = {
            "status": "accepted",
            "task_id": payload.get("task_id") or payload.get("payload", {}).get("issue_number") or "unknown",
            "repo": repo or ALLOWED_REPOS_RAW,
            "endpoint": "/api/v1/tasks/spawn",
        }
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    server.serve_forever()
