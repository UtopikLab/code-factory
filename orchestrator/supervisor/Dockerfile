# Orchestrator image for autonomous LangGraph supervisor loop.
# Builds a Python 3.11 container with LangGraph, OpenAI client, and GitHub integration.

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (CPU-only: no torch/transformers/langchain-openai)
RUN pip install --no-cache-dir \
    langgraph>=1.2,<2.0 \
    langchain-core>=0.3,<0.4 \
    langgraph-sdk>=0.4,<0.5 \
    fastapi>=0.115,<0.120 \
    uvicorn>=0.30,<0.33 \
    tenacity>=8.5,<9.0 \
    pydantic>=2.7,<2.10 \
    httpx>=0.27,<0.29 \
    requests>=2.31.0 \
    pyyaml>=6.0 \
    python-dotenv>=1.0 \
    aiohttp>=3.9

COPY supervisor_loop.py /opt/orchestrator/supervisor-loop.py

# Persistent loop state and task work directories
RUN mkdir -p /data/loop-state /data/task-work

# Health check (Phase D: readiness target)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

# Default entrypoint: resident supervisor loop
ENTRYPOINT ["langgraph", "cli", "serve"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
