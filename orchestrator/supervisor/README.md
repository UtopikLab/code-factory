# Orchestrator Supervisor Image

Orchestrator image for the autonomous LangGraph supervisor loop.

## Files
- `Dockerfile` — supervisor image build definition
- `requirements.txt` — Python dependencies
- `supervisor.py` — resident supervisor loop implementation

## Build
```bash
docker build -f orchestrator/supervisor/Dockerfile \
	-t ghcr.io/utopiklab/code-factory-supervisor:$TAG \
	orchestrator/supervisor
```

## Runtime behavior
- Container entrypoint is `python supervisor.py`.
- Process is resident (always-on) and serves:
	- `GET /healthz` on port `9000`
	- `GET /metrics` on port `9000`
- The supervisor remains idle between tasks and exposes Prometheus metrics for
	external scraping.
