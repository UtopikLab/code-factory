# Coder Worker image for autonomous LangGraph coder agents.
# Builds a Python 3.12 container with code execution, GitHub API, and OpenAI client.

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

# Create working directories
RUN mkdir -p /data/task-work /data/agent-cache /data/outputs

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default entrypoint: worker mode
ENTRYPOINT ["python3", "/app/supervisor_loop.py", "--worker"]

# Expose health check port
EXPOSE 8000
