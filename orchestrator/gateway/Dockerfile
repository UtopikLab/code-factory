# syntax: docker/dockerfile:1
# Gateway image for the always-on task-acceptor.
# Pure stdlib HTTP server (no pip installs): http.server + ThreadingHTTPServer on :8000.
FROM python:3.12-slim

COPY server.py /app/server.py

WORKDIR /app

# Default: accept tasks from ANY repository. Override at runtime with
#   kubectl set env deployment/k8s-inference-orchestrator \
#     -n orchestrator ALLOWED_REPOS="org/repo1,org/repo2"
ENV ALLOWED_REPOS="*"
ENV PORT=8000

EXPOSE 8000

CMD ["python3", "server.py"]
