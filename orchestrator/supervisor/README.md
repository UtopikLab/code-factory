# Orchestrator Supervisor Image

Orchestrator image for autonomous LangGraph supervisor loop. Builds a Python 3.11 container with LangGraph, OpenAI client, and GitHub integration.

## Files
- `Dockerfile` — supervisor image build definition
- `requirements.txt` — Python dependencies
- `supervisor_loop.py` — resident supervisor loop implementation

## Build
```
docker build -f supervisor/Dockerfile -t ghcr.io/utopiklab/langgraph-supervisor:$TAG .
```

## Supervisor loop entrypoint
The supervisor loop runs in two modes:
- **Resident loop** (default entrypoint): `langgraph cli serve` — the supervisor loop runs continuously inside the container.
- **`--worker` mode**: the supervisor loop runs as a detached worker process, managed externally.
