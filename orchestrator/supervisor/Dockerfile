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

WORKDIR /opt/orchestrator

# System dependencies (CPU-only toolchain).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from the build stage.
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin

# Supervisor loop source (modules).
COPY supervisor/ ./supervisor/

# Persist loop state and task work directories.
RUN mkdir -p /data/loop-state /data/task-work && \
    chown -R orchestrator:orchestrator /opt/orchestrator /data

# Health check (Phase D: readiness target).
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

USER orchestrator

# Default entrypoint: resident supervisor.
ENTRYPOINT ["langgraph", "cli", "serve"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
