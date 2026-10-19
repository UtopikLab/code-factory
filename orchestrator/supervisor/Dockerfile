# Supervisor image for the autonomous LangGraph supervisor loop.
# CPU-only: no torch/transformers/langchain-openai. Judge runs on a separate GPU pod.
# Multi-stage build: compile deps in a slim builder, then install into a runtime image.

# ---- Build stage: install CPU-only dependencies ----
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

ENV ACCEPTANCE_THRESHOLD="0.7"
ENV ACCEPTOR_URL="http://orchestrator.local:8000"
ENV JUDGE_MODEL="qwen2.5-coder-7b-instruct-q4-km@32k"
ENV JUDGE_URL="https://inference.local/v1"
ENV WORKER_COUNT="2"

# System dependencies (CPU-only toolchain).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from the build stage.
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin

# Supervisor loop source (modules).
COPY supervisor.py /app/supervisor.py

# Persist loop state and task work directories.
RUN mkdir -p /data/loop-state /data/task-work && \
    chown -R orchestrator:orchestrator /app /data

# Health check (Phase D: readiness target).
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

USER orchestrator

# Default entrypoint: resident supervisor loop.
ENTRYPOINT ["python", "supervisor.py"]
