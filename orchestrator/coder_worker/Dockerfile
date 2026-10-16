# Coder Worker image for autonomous LangGraph coder agents.
# Builds a Python 3.12 container with code execution, GitHub API, and OpenAI client.

FROM python:3.12-slim

WORKDIR /app
COPY worker.py /app/worker.py
COPY requirements.txt /app/requirements.txt

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (CPU-only: no torch/transformers/langchain-openai)
RUN pip install --no-cache-dir -r requirements.txt

# Working directories for code execution and persistent loop state
RUN mkdir -p /data/task-work /data/agent-cache /data/outputs /data/loop-state

# Health check (Phase D: readiness target)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

# Default entrypoint: worker mode
ENTRYPOINT ["python3", "worker.py", "--worker"]

# Expose health check port
EXPOSE 8000
