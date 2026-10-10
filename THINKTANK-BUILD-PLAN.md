# Sliced Build & Run Plan — LangGraph Orchestrator on `k8s-node01`

> Concrete, step-by-step build/run plan grounded in the actual code under
> `orchestrator/`, `inference/`, `monitoring/`. NOT the `thinktank-plan.md`
> design doc. Every command below maps to a real file / real manifest field.
>
> Target node: **k8s-node01** — K3s single-node, Intel Xeon Gold 6148 (80 cores,
> 91 GiB RAM), 2× Tesla P40 (23 GiB each, no tensor cores), containerd, `ctr` +
> `crictl` only (no docker / nerdctl on this box).
>
> Namespace: `orchestrator`. Supervisor image:
> `${IMAGE:-ghcr.io/UtopikLab/langgraph-orchestrator:latest}`.

---

## §0 — Scope & allowlist

### What this plan builds
- The **always-on CPU-only supervisor** (`orchestrator` Deployment) that holds
  the LangGraph state machine and hosts the `:8000` task-acceptor sidecar.
- The **on-demand coder-worker Job** template the supervisor renders per task.
- The **judge brain** (llama.cpp server on a GPU pod) that the loop calls over
  the network — treated as a prerequisite runtime service, not part of the
  orchestrator image.
- **Monitoring wiring** (node-exporter → central Prometheus on TrueNAS; iDRAC
  exporter).
- The **`langgraph-orchestrator` image**, built on a separate build host and
  pushed to `ghcr.io`.

### Approved allowlist (use ONLY these)
| Category | Approval | Scope |
|---|---|---|
| **3 — cluster tooling** | ✅ APPROVED | `kubectl`, `git` |
| **4 — infra services** | ✅ APPROVED | NVIDIA device-plugin DaemonSet, Traefik Ingress, containerd, K3s |
| **5 — external services** | ✅ APPROVED (official sources only) | llama.cpp (`ggerganov/llama.cpp` GitHub release binaries), GitHub, Prometheus+Grafana, iDRAC/IPMI (`ipmitool`), node-exporter (`prom/node-exporter`) |
| **6 — secrets** | ✅ NOTHING TO APPROVE | handled in-cluster only |
| **1 — build tooling** | ⛔ NOT APPROVED | buildah / docker / nerdctl — see §4 |

### Hard constraints (re-stated for the build)
1. **K8s is the runtime.** No rethink — the orchestrator is a CPU-only
   Deployment in `orchestrator`. The runtime runs fine in K8s.
2. **This box is a dedicated runtime server, NOT a build machine.** The image
   build+push is a **side job on a separate build host**. It must not run on
   `k8s-node01` (constraint §3 of the task brief).
3. **The image does not exist yet.** It must be built from `orchestrator/Dockerfile`.
4. **Only official sources; only LTS / stable / latest.** Do not widen the
   `requirements.txt` ranges.

### Pending decision (blocks nothing but must be chosen before Phase A)
- **How** the `${IMAGE}` is built: **buildah** on the node's containerd (but *not*
  on this dedicated box — buildah would have to run on a build host), **or** a
  build host with `docker` / `nerdctl`. The *decision* is what's pending, not
  whether an image can be produced.

---

## §1 — Prerequisites

Run everything here **except** Phase A (image build), which runs on a **separate
build host**.

### 1.1 Cluster connectivity
```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml      # per k8s-node01.md §3
kubectl version --client && kubectl cluster-info
kubectl get nodes -o wide                         # confirm k8s-node01 Ready
```

### 1.2 Build host for the image (a *different* machine than k8s-node01)
The build host needs `buildah` (or `docker`/`nerdctl` + containerd) and registry
write access to `ghcr.io`. Do **not** perform the build on k8s-node01.

### 1.3 Secrets already on the cluster
The terminal log shows this was attempted (values **masked** here):
```bash
kubectl create secret generic orchestrator-secrets \
  --namespace orchestrator \
  --from-literal=openai-api-key=sk-1x...MASKED... \
  --from-literal=github-token=github_pat_11AB...MASKED...
```
- **Verify it exists** (`kubectl get secret orchestrator-secrets -n orchestrator -o yaml`).
- **⚠️ The `github-token` value in that secret is the expired PAT** (see §5).
  Before any task can be accepted the secret must be refreshed — do **not**
  persist or echo the real values; recreate from the rotated PAT.
- Also create the iDRAC secret (`monitoring/secrets.yaml`):
  ```bash
  kubectl create secret generic idrac-credentials --namespace monitoring \
    --from-literal=password=<the-idrac-password>
  ```

### 1.4 Judge brain (llama.cpp) — prerequisite runtime service
The supervisor and agent Jobs reach the judge at
`https://llm.local/v1` (host `llm-server-gpu1`). It must be **running** before a
task can complete. This is the existing `inference/` stack — ensure it is
deployed and serving (the deploy script covers it, see §2 phase F):
```bash
kubectl wait --for=condition=ready pod --all -n llm --timeout=180s
curl -fsS https://llm.local/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5-coder-7b-instruct-q5-kn@32k","messages":[]}'
```

---

## §2 — Sliced build steps (dependency order)

### Phase A — Build & push the image on a SEPARATE build host ⛔ not k8s-node01

**Option 1 — buildah (builds directly from the Dockerfile):**
```bash
# On the build host (NOT k8s-node01). git is approved to fetch the source.
git clone <repo-url> && cd k8s
cd orchestrator

# Authenticate to GHCR with the official GH PAT.
buildah login -u <gh-user> -p <gh-pat> ghcr.io

# Build from the real Dockerfile (FROM python:3.11-slim + requirements.txt +
# agent-job-template.yaml + supervisor-task-acceptor.py baked to /opt/orchestrator).
buildah bud -f Dockerfile -t ghcr.io/UtopikLab/langgraph-orchestrator:latest .

# Push to the registry.
buildah push ghcr.io/UtopikLab/langgraph-orchestrator:latest \
  ghcr.io/UtopikLab/langgraph-orchestrator:latest

# Verify.
buildah from ghcr.io/UtopikLab/langgraph-orchestrator:latest
buildah run orchestrator-<digest> ls -l /opt/orchestrator
```

**Option 2 — docker / nerdctl (build host with a containerd backend):**
```bash
# On the build host (NOT k8s-node01).
git clone <repo-url> && cd k8s/orchestrator
docker build -t ghcr.io/UtopikLab/langgraph-orchestrator:latest .

docker login ghcr.io -u <gh-user> -p <gh-pat>
docker push ghcr.io/UtopikLab/langgraph-orchestrator:latest
```
> `nerdctl` is a drop-in replacement for `docker` in both snippets.

**Result:** `ghcr.io/UtopikLab/langgraph-orchestrator:latest` exists and is
CPU-only (no torch/transformers/langchain-openai — the judge runs elsewhere).

### Phase B — Cluster prerequisites (namespace, ConfigMap, Secret)
```bash
# Namespace
kubectl create namespace orchestrator
kubectl apply -f orchestrator/namespace.yaml

# ConfigMap orchestrator-config (policy knobs — taken from workers.yaml data).
kubectl create configmap orchestrator-config --namespace orchestrator --dry-run=client -o yaml \
  | kubectl apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: orchestrator-config
  namespace: orchestrator
  labels:
    app: orchestrator
data:
  max-iterations: "8"
  task-wait-seconds: "30"
  loop-state-dir: "/data/loop-state"
  judge-url: "https://llm.local/v1"
  judge-model: "qwen2.5-coder-7b-instruct-q5-kn@32k"
EOF

# Secrets (refresh the expired PAT before running this).
kubectl create secret generic orchestrator-secrets --namespace orchestrator \
  --from-literal=openai-api-key=<the-openai-key> \
  --from-literal=github-token=<the-rotated-pat>
```

### Phase C — Deploy the orchestrator stack
```bash
# Supervisor Deployment + in-cluster Service (port 8000).
kubectl apply -f orchestrator/supervisor.yaml

# Worker Job template (ConfigMap + coder-workers Job; agent-job-template.yaml
# lives in the image, so it is NOT applied here — the acceptor renders it).
kubectl apply -f orchestrator/workers.yaml

# Traefik Ingress so the supervisor Service is reachable for the first task.
# (Traefik Ingress is Category 4 / APPROVED.) Point Traefik at the
# orchestrator Service (orchestrator.orchestrator.svc:8000) and expose it on a
# routable host. The drain.yaml workflow defaults to the in-cluster URL
# https://supervisor.orchestrator.svc.cluster.local:8000.
```

### Phase D — Validate
```bash
# Pods reach Ready.
kubectl -n orchestrator wait --for=condition=ready pod -l app=orchestrator \
  --timeout=180s
kubectl get pods -n orchestrator -o wide

# Service is up and points at the pod.
kubectl get service orchestrator -n orchestrator -o wide

# Readiness probe target: the acceptor HTTP server on :8000.
kubectl -n orchestrator exec orchestrator-<pod> -- sh -c \
  'curl -fsS http://localhost:8000/healthz && echo "healthz OK"'

# Liveness probe target: the resident LangGraph loop.
kubectl -n orchestrator exec orchestrator-<pod> -- sh -c \
  'pgrep -f langgraph >/dev/null && echo "langgraph resident" || echo "langgraph NOT resident"'

# Accepter auth round-trip (dry, anonymous GET to confirm the port is bound).
kubectl -n orchestrator exec orchestrator-<pod> -- sh -c \
  'curl -fsS http://localhost:8000/healthz'
```

### Phase E — Monitoring wiring (node-exporter, iDRAC)
Both manifests already exist in `monitoring/`; ensure they are applied and add
the central-Prometheus (TrueNAS) wiring.

```bash
# node-exporter DaemonSet -> host :9100 (whole-box CPU/RAM/GPU/disk metrics).
kubectl apply -f monitoring/node-exporter.yaml

# iDRAC exporter -> central Prometheus (transport depends on the iDRAC version,
# Open Question O3; default manifest assumes redfish).
kubectl apply -f monitoring/idrac.yaml
```

Central Prometheus on TrueNAS (`http://truenas.utopiklab.lan:30104/`):
1. Add a scrape job for the node-exporter metrics endpoint. Because the
   DaemonSet uses `hostNetwork`/`hostPID`, the scrape target is the node's own
   IP on `:9100`:
   ```yaml
   apiVersion: v1
   kind: ConfigMap
   metadata:
     name: prometheus-node-exporter
     namespace: prometheus
     labels:
       prometheus: central
   data:
     prometheus.yml: |
       scrape_configs:
         - job_name: 'k8s-node01-node-exporter'
           kubernetes_sd_configs: []
           static_configs:
             - targets: ['<k8s-node01-ip>:9100']
           metrics_path: /metrics
           scheme: http
   ```
2. Add an iDRAC hardware-health scrape job (the `grafana/redfish-exporter`
   DaemonSet already remote-writes to Prometheus; if the iDRAC is IPMI (O3),
   use the official `ipmi-exporter` (Andy Polyakov) instead of redfish).
3. Grafana (official) on TrueNAS: add a data source pointing at the Prometheus
   instance and import a node-exporter dashboard.

### Phase F — Trigger the first task
**Option 1 — via the GitHub Actions trigger (`.github/workflows/drain.yaml`):**
push a branch that opens/assigns an issue on the scoped repo. The workflow POSTs
to the supervisor with the workflow's own `$GITHUB_TOKEN`. No extra secret.

**Option 2 — manual curl to the supervisor (needs the ingress from Phase C):**
```bash
curl -fsS -X POST "https://<ingress-host>/api/v1/tasks/spawn" \
  -H "Authorization: token <the-pat>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "orchestrator-agent",
    "ref": "refs/heads/main",
    "payload": {
      "issue_number": 42,
      "issue_title": "Fix the thing",
      "issue_url": "https://github.com/UtopikLab/llm-rig/issues/42",
      "repo": "UtopikLab/llm-rig",
      "default_branch": "main"
    }
  }'
```
The acceptor (see `supervisor-task-acceptor.py`) will: validate the token,
refuse the task if `repo` is out of the PAT's scope, render
`agent-job-template.yaml` (`${TASK_ID}`, `${ISSUE_URL}`, `${REPO}`,
`${GITHUB_TOKEN}`), `kubectl apply`, `kubectl wait --for=jobcomplete`, reap the
Job, and return `{"uid":..., "task_id":"UtopikLab/llm-rig#42", ...}`.

---

## §3 — Version / source table

| Component | Reference / version | Source (official) | LTS / stable pin |
|---|---|---|---|
| Image base | `python:3.11-slim` | Docker Hub (official) | `3.11` (LangGraph requires 3.11+) |
| langgraph | `>=1.2,<2.0` | PyPI | latest `1.x` |
| langgraph-sdk | `>=0.4,<0.5` | PyPI | latest in range |
| langchain-core | `>=0.3,<0.4` | PyPI | latest in range |
| fastapi | `>=0.115,<0.120` | PyPI | latest in range |
| uvicorn | `>=0.30,<0.33` | PyPI | latest in range |
| tenacity | `>=8.5,<9.0` | PyPI | latest in range |
| pydantic | `>=2.7,<2.10` | PyPI | latest in range |
| httpx | `>=0.27,<0.29` | PyPI | latest in range |
| node-exporter | `v1.9.1` | `prom/node-exporter` (official) | pinned `v1.9.1` (stable) |
| redfish-exporter | `:latest` | `grafana/redfish-exporter` (official) | `:latest` (O8-adjacent still-open pin) |
| llama.cpp | `:latest` | `ghcr.io/ggerganov/llama.cpp` (official registry) | `:latest` (official release build) |
| K3s | current stable | `k3s.io` (official) | stable channel |
| containerd | current stable | `containerd.io` (official) | stable |
| Traefik | current stable | `traefik.io` (official) | stable |
| NVIDIA device-plugin | current stable | `nvidia-k8s/nvidia-device-plugin` (official) | stable |
| ipmitool | current stable | `lm-sensors/ipmi-exporter` (official) | stable (IPMI path) |
| Prometheus | current stable | `prometheus/prometheus` (official) | stable |
| Grafana | current stable | `grafana/grafana` (official) | stable |
| kubectl | current stable | `kubernetes/sigs.k8s.io/cli-utils` (official) | stable |
| git | current stable | `git` (official) | stable |

> No torch / transformers / langchain-openai anywhere in this table — the
> orchestrator image is CPU-only by design.

---

## §4 — Blocked / needs-approval

- **Category 1 — build tooling (NOT yet approved):** the choice of *how* the
  `${IMAGE}` is built. Options:
  1. **buildah** — available on the build host; builds directly from the
     Dockerfile and can push to `ghcr.io`. Do **not** run buildah on
     k8s-node01 (this is a dedicated runtime box).
  2. **docker / nerdctl** on a build host with a containerd backend.
  Either option keeps the image build off the runtime node. **Decision pending.**

- **iDRAC version (Open Question O3):** the iDRAC transport in
  `monitoring/idrac.yaml` defaults to `redfish`. Confirm the actual iDRAC
  version on k8s-node01; if it is older, switch to the IPMI path (`ipmitool`
  → `ipmi-exporter`).

- **Ingress exposure:** Phase C assumes a Traefik Ingress exposing the
  orchestrator Service. Confirm the routable host / domain before the manual
  curl trigger (Option 2, §2 Phase F). The GitHub Actions trigger uses the
  in-cluster URL and needs no ingress.

---

## §5 — Rollback / gotchas

- **PAT expiry (critical):** the PAT in `orchestrator-secrets`
  (`github_pat_11ABRIFCY0...`) **expired 2026-08-08**. Today (2026-10-10) the
  acceptor will reject every task with `401 invalid token`. **Refresh the PAT
  and recreate `orchestrator-secrets` before Phase F.** The new PAT must match
  the value the `drain.yaml` workflow injects (`${{ secrets.GITHUB_TOKEN }}`);
  they must be identical or the acceptor's exact-token check fails.
- **Repo scope:** the acceptor refuses any task whose `payload.repo` is not in
  the PAT's scoped scope. Keep the workflow PAT repo-scoped (it is:
  `contents/actions/issues: write`).
- **Context window pinned @32768:** the judge model is
  `qwen2.5-coder-7b-instruct-q5-kn@32k` (from `orchestrator-config`). The `@32k`
  locks the 32768-token context window for both the supervisor and agent brains.
  Do not bump to `@40k`/`@64k` without re-checking node RAM/CPU headroom.
- **GPU pinned explicitly:** the llama.cpp server pods use
  `nvidia.com/gpu-device-id: "0"` / `"1"` (see `inference/deployment.yaml`).
  Never let the orchestrator (CPU-only, no GPU request/limit) schedule onto a
  GPU — it has none requested.
- **Build must not run on the node:** the image build+push (Phase A) is a side
  job on a build host. Keep k8s-node01's `ctr`/`crictl`-only containerd clean.
- **Port 8000 is the acceptor's only HTTP surface:** readiness = `GET
  /healthz`; liveness = `pgrep -f langgraph`. A restart of the container
  re-runs `langgraph.cli serve` which re-launches the loop *and* rebinds the
  acceptor on the next task (the sidecar re-enters per task). Do not deploy a
  second listener on :8000.
- **Doc discrepancy — flag, do not fix here:** `inference/deployment.yaml`
  (and the inline comment in `orchestrator/supervisor.yaml`) describe the judge
  brain as **Qwen2.5-14B-Instruct-AWQ** (GPU 0) / **Qwen2.5-7B-Instruct-AWQ**
  (GPU 1), `--model-format awq`, `--ctx-size 4096`, `f8_e4m3` tensors. The
  **actual baseline** judge model wired into the orchestrator is
  **`qwen2.5-coder-7b-instruct-q5-kn`** — a **GGUF q5_kn** quantization with a
  **32k** context window. The two are inconsistent (AWQ vs GGUF; 7B/14B vs
  7B-Coder; 4096 vs 32768 ctx). This plan does **not** resolve it; refresh
  `inference/deployment.yaml` / `supervisor.yaml` comments to match the real
  GGUF q5_kn @32k baseline before relying on the judge.
