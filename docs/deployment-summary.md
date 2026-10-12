# k8s-node01 Orchestrator Deployment Summary

**Status:** ✅ UP AND RUNNING

**Date Deployed:** 2026-10-11  
**Architecture:** Keep-vs-defer minimal loop  
**Deployment Model:** 24/7 resident always-on service

---

## Quick Status

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# Check cluster health
kubectl get nodes
kubectl get pods -A | grep -E "Running|orchestrator|inference"

# Check supervisor logs
kubectl logs -n orchestrator -l app=orchestrator -c supervisor --tail=20

# Test task-acceptor
kubectl exec -n orchestrator -l app=orchestrator -c task-acceptor -- \
  python3 -c "import http.client; c=http.client.HTTPConnection('localhost',8000); \
  c.request('GET','/'); print(c.getresponse().read().decode())"
```

---

## Architecture (Keep-vs-Defer)

The deployed system implements the minimal credible loop:

| Component | Role | Status | Notes |
|---|---|---|---|
| **Supervisor** | Always-on planner/coordinator | ✅ Running (CPU-only) | `k8s-inference-orchestrator` deployment |
| **Coder Brain** | P40-0 inference server (Qwen2.5-7B) | ✅ Running | Interactive + worker calls |
| **Judge Brain** | P40-1 inference server (Qwen2.5-7B) | ✅ Running | Evaluates worker outputs |
| **Workers** | Spawned on-demand (K8s Jobs) | ✅ Ready | Coder agents for task execution |
| **Task-Acceptor** | HTTP endpoint :8000 | ✅ Running | Receives tasks via webhook/API |
| **Human Gate** | GitHub PR review | ✅ Configured | No auto-merge |
| **Monitoring** | Node-exporter → central Prometheus | ✅ Running | Host metrics collected |

### Deferred (Not in first build)
- Dedicated verifier agent
- Separate PR writer
- Additional specialist roles
- Larger planner models by default

---

## Namespaces

| Namespace | Purpose | Pods | Status |
|---|---|---|---|
| `orchestrator` | Supervisor + task-acceptor + worker jobs | 1 deployment + completed jobs | ✅ Ready |
| `inference` | GPU inference servers (coder + judge) | 2 deployments (llama-server-args-gpu0/1) | ✅ Running |
| `gpu` | NVIDIA device-plugin | 1 daemonset | ✅ Ready |
| `monitoring` | Node-exporter + telemetry | 1 daemonset (node-exporter), 1 daemonset (IPMI, erroring) | ⚠️ Partial |

---

## Resource Allocation

### GPU Budget
- **P40-0** (23 GB VRAM): Qwen2.5-Coder-7B-Instruct @ Q4_K_M (~4.7 GB) + inference overhead
- **P40-1** (23 GB VRAM): Qwen2.5-Coder-7B-Instruct @ Q4_K_M (~4.7 GB, judge) + overhead
- **Headroom**: ~18 GB spare per card for batch processing

### CPU/Memory Budget
- **Supervisor**: CPU `500m` req / `2500m` limit, Memory `2Gi` req / `5Gi` limit
- **Task-Acceptor**: CPU `100m` req / `250m` limit, Memory `256Mi` req / `512Mi` limit
- **Workers**: CPU-only, spawned on demand with configured limits
- **Node Total**: 80 cores, 91 GiB RAM → ample headroom

---

## Configuration

### Supervisor Loop Policy
- **MAX_ITERATIONS:** 8 (bounded retry budget)
- **WORKER_COUNT:** 2 (parallel worker jobs per task)
- **ACCEPTANCE_THRESHOLD:** 0.7 (score cutoff for passing outputs)
- **JUDGE_URL:** https://inference.local/v1 (OpenAI-compatible endpoint)
- **JUDGE_MODEL:** qwen2.5-coder-7b-instruct-q5-kn@32k

All configurable via ConfigMap: `kubectl edit configmap k8s-inference-orchestrator-config -n orchestrator`

### Secrets
- **OPENAI_API_KEY:** Required for judge inference calls
- **GITHUB_TOKEN:** Required for task-acceptor authorization + PR operations

Stored in: `kubectl get secret k8s-inference-orchestrator-secrets -n orchestrator`

---

## Deployment Flow

```
GitHub Issue/PR
      ↓
GitHub Actions (drain.yaml)
      ↓
POST /api/v1/tasks/spawn (auth: token $GITHUB_TOKEN)
      ↓
task-acceptor sidecar (port 8000)
      ↓
Supervisor Loop:
  1. PLAN: Read issue, set iteration budget
  2. SPAWN: N parallel coder workers (K8s Jobs)
  3. EXECUTE: Each worker edits repo, runs tests
  4. EVALUATE: Judge scores each output
  5. PICK BEST: Select highest-scoring patch
  6. RETRY or GATE: Loop up to MAX_ITERATIONS or proceed to human review
  7. GATE: Open PR for human approval (no auto-merge)
      ↓
Human Reviews PR
      ↓
Merge / Reject
```

---

## Monitoring

### Metrics Collected
- **Host metrics** (node-exporter → central Prometheus on TrueNAS)
  - CPU utilization per-core + aggregate
  - Memory (total/free/cache)
  - Disk usage + I/O
  - GPU utilization + VRAM (if exposed)

- **Pod metrics** (K8s metrics-server)
  - Per-pod CPU/memory
  - Container status

### Central Prometheus
- **Endpoint:** http://truenas.utopiklab.lan:30104/
- **Scrape target:** node-exporter on k8s-node01:9100

### Grafana Dashboards
- Add data source pointing to central Prometheus
- Import node-exporter dashboard
- Build custom orchestrator dashboard (optional)

### Known Issues
- iDRAC/IPMI exporter pod in CrashLoopBackOff (config format issue) — not critical for core loop

---

## How to Trigger a Task

### Option 1: GitHub Actions Workflow (automatic)
Push a branch that opens/assigns an issue on the repo:
```bash
git push origin feature/your-feature
# → GitHub Actions runs drain.yaml
# → POST to supervisor task-acceptor
# → Orchestrator processes the task
```

### Option 2: Manual cURL (for testing)
```bash
export GITHUB_TOKEN=<your-pat>

curl -X POST http://supervisor.orchestrator.svc.cluster.local:8000/api/v1/tasks/spawn \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "test-001",
    "repo": "UtopikLab/llm-rig",
    "issue_number": 123,
    "issue_title": "Fix: broken test",
    "issue_url": "https://github.com/UtopikLab/llm-rig/issues/123"
  }'
```

Expected response:
```json
{
  "status": "accepted",
  "task_id": "test-001",
  "repo": "UtopikLab/llm-rig",
  "endpoint": "/api/v1/tasks/spawn"
}
```

---

## Verification Checklist

✅ K3s cluster ready (1 node)  
✅ GPU servers running (P40-0 coder, P40-1 judge)  
✅ Supervisor always-on (1/1 replicas)  
✅ Task-acceptor listening (:8000)  
✅ Secrets configured  
✅ ConfigMap set (loop policy)  
✅ Node-exporter running  
✅ Worker job template ready  
✅ Resource limits set (no starvation)  
✅ Human PR gate configured  

---

## Next Steps

1. **Test the first task**: Push an issue to the repo and verify the orchestrator processes it
2. **Monitor the logs**: `kubectl logs -n orchestrator -l app=orchestrator -f`
3. **Review the PR**: Supervisor will open a PR for human review when a candidate passes
4. **Tune the parameters**: Adjust MAX_ITERATIONS, WORKER_COUNT, ACCEPTANCE_THRESHOLD based on observed behavior
5. **Expand carefully**: Only add specialist roles (verifier, PR writer, etc.) when evidence shows they are needed

---

## Troubleshooting

### Supervisor pod CrashLoopBackOff
```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl logs -n orchestrator k8s-inference-orchestrator-<pod-id> -c supervisor --tail=50
```
Check that secrets are correctly configured and the image is available.

### Task-acceptor not responding
```bash
kubectl exec -n orchestrator <pod-id> -c task-acceptor -- \
  python3 -c "import http.client; c=http.client.HTTPConnection('localhost',8000); \
  c.request('GET','/'); print(c.getresponse().read())"
```
Verify the sidecar container is ready and port 8000 is bound.

### Workers not spawning
```bash
kubectl get jobs -n orchestrator
kubectl describe job <job-name> -n orchestrator
```
Check that the worker job template is valid and secrets are available.

### GPU servers not responding
```bash
kubectl get pods -n inference
kubectl logs -n inference llama-server-args-gpu0-* --tail=50
```
Verify the inference stack is running and the OpenAI-compatible endpoint is responding.

---

## Architecture Reference

For the full design intent and open questions, see:
- [`docs/thinktank-plan.md`](../thinktank-plan.md) — source-of-truth design
- [`docs/keep-vs-defer-architecture.md`](../keep-vs-defer-architecture.md) — repo implementation posture
- [`docs/build-plan.md`](../build-plan.md) — concrete build steps

---

**Deployed by:** Orchestrator Agent  
**Last updated:** 2026-10-11 23:52 UTC  
**System ready for autonomous operation.**
