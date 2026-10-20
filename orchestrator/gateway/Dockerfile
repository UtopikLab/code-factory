# syntax: docker/dockerfile:1
# Gateway image for the always-on task-acceptor.
# The HTTP acceptor uses the stdlib http.server (no pip installs), but server.py
# imports prometheus_client for the cfe_-prefixed metrics on :9000, so this image
# uses a multi-stage build to install prometheus_client.
FROM python:3.12-slim AS build

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ---- Runtime image ----
FROM python:3.12-slim AS runtime

# Non-root user (security hardening).
RUN groupadd --gid 1001 orchestrator && \
    useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash orchestrator

WORKDIR /app

ENV ALLOWED_REPOS="*"
ENV PORT=8000

# System dependencies.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from the build stage.
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin

# Gateway source.
COPY server.py /app/server.py

# Working directory for HTTP listeners and prometheus /metrics.
RUN mkdir -p /data/listeners /data/tasks && \
    chown -R orchestrator:orchestrator /app /data

EXPOSE 8000

CMD ["python3", "server.py"]
