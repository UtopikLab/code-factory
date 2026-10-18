# syntax: docker/dockerfile:1
# Gateway image for the always-on task-acceptor.
# Pure stdlib HTTP server (no pip installs): http.server + ThreadingHTTPServer on :8000.
FROM python:3.12-slim
COPY server.py /app/server.py
WORKDIR /app
ENV PORT=8000
EXPOSE 8000
CMD ["python3", "server.py"]
