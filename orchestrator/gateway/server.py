import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("GITHUB_TOKEN", "")
ALLOWED_REPO = os.environ.get("ALLOWED_REPO", "UtopikLab/code-factory")


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
        if repo and repo != ALLOWED_REPO:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error":"repo-not-allowed"}')
            return

        response = {
            "status": "accepted",
            "task_id": payload.get("task_id") or payload.get("payload", {}).get("issue_number") or "unknown",
            "repo": repo or ALLOWED_REPO,
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
