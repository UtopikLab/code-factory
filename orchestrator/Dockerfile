# Orchestrator image for autonomous LangGraph supervisor loop.
# Builds a Python 3.12 container with LangGraph, OpenAI client, and GitHub integration.

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir \
    langgraph \
    langchain \
    langchain-openai \
    requests \
    httpx \
    pydantic \
    pyyaml \
    python-dotenv \
    aiohttp

# Copy supervisor loop code
COPY supervisor_loop.py /app/supervisor_loop.py

# Create data directories for persistent state
RUN mkdir -p /data/loop-state /data/task-work

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default entrypoint: supervisor loop
ENTRYPOINT ["python3", "/app/supervisor_loop.py"]
