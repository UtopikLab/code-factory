# k8s-node01 Orchestrator Deployment Summary

**Status:** Partial recovery; gateway HTTPS intake remains blocked (2026-10-21).

## Supervisor Catalog Permission And Callback DNS: Prepared, Not Deployed

Read-only checks on 2026-10-22 confirmed task
`issue-2e2948ebe051a6f6e47ce755478b7a47` is `running` at attempt 8, pinned to
`orchestrator-agent-catalog-2d50f5f5368d` in namespace `orchestrator`.
`kubectl auth can-i get configmap/orchestrator-agent-catalog-2d50f5f5368d -n orchestrator --as=system:serviceaccount:orchestrator:orchestrator-supervisor`
returned `no`; a GET from the supervisor pod returned HTTP 403 Forbidden for
that same service account, resource and namespace. The live and checked-in
supervisor Roles had only job permissions, not a stale catalog allowlist.

The chart now grants the supervisor namespace-scoped ConfigMap `get` access
through its existing Role and RoleBinding, without a manual catalog hash inventory.
This deliberately allows reading any ConfigMap whose name the supervisor knows
in its namespace, not only catalogs. The chart's ConfigMaps contain configuration,
not credentials; credentials use Secrets. No ConfigMap list/watch/write, Secret
read, or cluster-scoped permissions are added. Existing namespace-wide Job
permissions are unchanged.

Catalog names still use the first 12 hash characters; catalog/runtime/schema
content changes produce new names, and Helm keeps historical immutable ConfigMaps.
Tasks continue loading their pinned catalogs by name with content-hash validation;
no runtime pinning, stored catalogs, or task records are changed.

The supervisor pod's current `ACCEPTOR_URL` and this task's pinned runtime both
use `https://code-factory.local`. Pod DNS returned errno -2 for that hostname;
`code-factory.utopiklab.com` and the actual internal gateway Service resolved.
A TLS-verified, unauthenticated GET to the public HTTPS task-list endpoint
returned HTTP 401, as did the internal gateway. This proves reachability, not
authenticated callback success. The ignored local values now also set
`supervisor.config.acceptor-url: https://code-factory.utopiklab.com`, preserving
the HTTPS schema policy. No application TLS or authentication checks changed.

Callback selection matters: before a catalog loads, terminal completion falls
back to the pod's `ACCEPTOR_URL`; after it loads, the pinned runtime's
`acceptor_url` takes precedence. Changing management's URL or generating a new
catalog does not repair old task snapshots. Even after the prepared RBAC and
URL changes are applied, this task still needs an approved compatibility plan:
restore DNS/routing with a valid TLS certificate for its old HTTPS origin, or
design and validate an explicit stored-task migration/recovery. Do not edit
catalog contents in place: their content hash is verified. Do not silently
rewrite task pins or retry this exhausted task.

Live log markers show eight catalog failures from 00:32:58.619 to 00:32:58.694,
then failed terminal persistence at 00:32:58.707. Nonterminal failures requeue
immediately, so the loop consumes eight attempts rapidly. Failed terminal
persistence leaves the task `running` with its claim; subsequent recovery waits
for the default 900-second lease (plus idle polling), not an endless 10 ms loop.
Regression tests cover callback precedence and this exact failure sequence;
retry behavior is unchanged.

No live resources, privileges, task records or deployments were changed.
After explicit approval and review of unrelated pending chart changes, the
orchestrator-only upgrade command is:

```bash
helm upgrade orchestrator helm/orchestrator -n orchestrator \
  -f helm/orchestrator/local.values.yaml -f helm/local.secrets-values.yaml
```

This command was not executed. If a shared nonsecret values file is used, layer
it before the orchestrator-local file, matching the deployment script. Do not
use `helm/deploy.sh` for this narrow action: it also deploys inference and deletes
Jobs/pods. An upgrade is not by itself a stored-task compatibility fix; the worker
Job may also expose existing immutable-field constraints. Do not delete it as
part of this incident without separate approval.

## Management Task Listing: Gateway URL Correction Prepared

Read-only checks on 2026-10-22 reproduced authenticated
`GET /management/api/tasks?limit=25&offset=0` directly against management:
HTTP 502 with `{"detail":"gateway-unavailable"}`. Its configured gateway URL
is `https://code-factory.local`; the application's HTTPX client fails with
`ConnectError` caused by DNS `gaierror` (errno -2). This is the currently verified
failure, not the upstream 401 observed earlier.

From the same management pod, using the existing gateway token without exposing
it, both the internal gateway Service and the TLS-verified public endpoint
`https://code-factory.utopiklab.com/api/v1/tasks?limit=25&offset=0` return HTTP 200
with the task-list response shape. The local, ignored override
`helm/orchestrator/local.values.yaml` now explicitly sets `management.gatewayUrl`
to `https://code-factory.utopiklab.com`. This URL correction is not deployed;
applying it requires approval. No application authentication or TLS checks were
weakened. Non-loopback HTTP gateway URLs are rejected by both the management
application and the chart schema, so the internal HTTP Service is not a valid
configuration-only alternative under the current policy.

Live Ingress metadata now shows `code-factory.local` on Traefik `web`, with
`/api/v1` routed to the gateway and `/` to management. Thus the earlier shared
Ingress routing change is already present live and does not fix the current DNS
failure. Preserve it; this URL correction does not require another routing change.
An unauthenticated request to the exact public HTTP management path returns
HTTP 401 with a Basic challenge. Credentials were not sent over public HTTP;
operators should use the HTTPS management origin.

## Optional Management UI Ingress

The orchestrator chart renders a management Ingress only when both
`management.enabled` and `management.ingress.enabled` are true. Both default
to false; the default ingress host and TLS secret name are empty.
The route is `/` with `pathType: Prefix` to the existing
`orchestrator-management` Service on port 8081, covering the UI, assets, and
same-origin `/management/api/*` endpoints without path rewriting.
When the effective `management.gatewayUrl` (falling back to
`supervisor.config.acceptor-url`) matches `management.publicOrigin`, ignoring
a trailing slash, this Ingress also routes `/api/v1` to the gateway on port 8000.
This routes shared-origin task listing to the gateway when the external proxy
selects the management Ingress host and entrypoint. Previously that Ingress only
had `/` pointing to management on port 8081. Management sends gateway requests
with `Authorization: token ...`; management's Basic/OIDC authentication rejects
that scheme, whereas the gateway accepts it with the matching gateway token.
An upstream 401 is translated by management into HTTP 502.
The gateway Ingress already has its own `/api/v1` route: without evidence of the
live proxy's selected host, entrypoint, and backend, misrouting remains a proposed
cause of the observed 401, not an established incident root cause.
Separate gateway origins retain their existing routing.
The external proxy must forward these requests to this Ingress; the chart does
not configure that proxy. See the current observations above for live routing.

A dedicated management host, distinct from `gateway.ingress.host`, is also supported.
The disabled example in `helm/orchestrator/example.values.yaml` uses
`management.example.com` with external TLS termination; it does not enable
the workload or ingress. Before enabling both settings:

- Supply a registry-verified, explicitly versioned management image. Set
  `management.repositories`, OIDC issuer/audience/JWKS URL and allowed subjects,
  and gateway credential values in `helm/local.secrets-values.yaml`; Helm creates
  the repository and authentication Secrets. No image or credentials
  are supplied by this ingress change.
- Set `management.publicOrigin` to the browser-visible HTTPS origin, without
  a path or trailing slash. Normally its hostname matches the ingress host;
  if an external TLS proxy rewrites Host, set the ingress host to the forwarded
  hostname while retaining the public HTTPS origin.
- For `management.ingress.tlsTermination: internal` (the default), provide
  `management.ingress.tlsSecretName` for a pre-provisioned certificate Secret
  in the chart namespace. This uses Traefik `websecure`, router TLS, and
  `spec.tls`, matching the gateway convention.
- For `external`, terminate HTTPS at the trusted proxy and forward to Traefik
  `web`; no ingress TLS block is rendered. Restrict direct HTTP backend access.
  Provision DNS/proxy routing and confirm the existing Traefik entrypoints
  before deployment. This change does not install a controller or certificates.

The original ingress preparation used only local Helm rendering/tests. The
read-only observations above establish the current task-listing failure and
working HTTPS gateway endpoint, not overall deployment readiness.

## Requester-Selected Native Models: Prepared, Not Deployed

Inference starts in llama.cpp **router mode without a model**, HF repository,
alias, preset whitelist, or custom downloader. The pinned official CUDA image
is unchanged: b11783, digest
`sha256:1bacc872b168d2d10bfd8c220336f5a2cd464c07605936ec146ee5198832ffce`.
The b11783 tag resolves to source commit
`339f3242c761f16c27e5ffcde1d35dcc31933a55`.

**Important native limitation:** a new HF ID in chat alone does NOT download
it. `server-models.cpp` rejects missing model metadata before autoloading.
`--models-dir` scans local files, not HF, and single-model `--hf-repo` does
not select models dynamically. Presets can name HF sources for lazy loading,
but require operator registration and are not arbitrary requester selection.

The worker instead uses the native two-step API, for either coder or judge:

1. `GET /models` discovers the requested ID (or an existing alias).
2. If absent or incomplete, `POST /models` with
   `{"model":"Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M"}` asks llama.cpp
   itself to download it. The response `{"success":true}` means accepted,
   not completed. There is no model allowlist in the chart or worker.
3. Poll `GET /models` until the entry is `unloaded`, `loaded`, or `sleeping`;
   `downloading` and `downloaded` are intermediate states. Failures are surfaced,
   interrupted downloads can resume, and the preparation deadline is 30 minutes.
4. `POST /v1/chat/completions` with the **same** model identifier, for example:
   `{"model":"Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M","messages":[{"role":"user","content":"ping"}]}`.
   Native autoload loads a discovered model and routes the request. Native-mode
   chat uses a separate 30-minute HTTP timeout to allow a cold load.

Other callers must perform that preparation themselves; chat-only arbitrary
HF download is not implemented by the pinned server. The native alternative is
`POST /models` plus `/models/sse` (`download_finished`/`download_failed`), then
`GET /models` and chat. No custom inference proxy was added.

The requester chooses `worker.worker-model` and
`supervisor.config.judge-model`; the default for both is the HF ID above.
`worker.coderUrl` and `supervisor.config.judge-url` still override endpoints.
Default endpoints now use separate Services for the existing deployments:
`llama-server-args-gpu0.inference.svc.cluster.local:8000/v1` for coder and
`llama-server-args-gpu1.inference.svc.cluster.local:8000/v1` for judge.
Preparation and chat MUST reach the same router. The old shared Service and
Ingress remain, but cannot guarantee this across their independent caches.
Custom deployment names require matching requester endpoint overrides.

`worker.coderNativeDownload` and `worker.judgeNativeDownload` default to null:
auto-enable only for their respective default router URLs. External URLs retain
ordinary OpenAI-compatible chat without native API calls; explicitly set the
corresponding flag to true for a custom single-router endpoint, or false to
disable preparation. Standalone Python workers default these flags to false
(`CODER_NATIVE_DOWNLOAD` / `JUDGE_NATIVE_DOWNLOAD`). Existing external-provider
model strings are preserved; old local aliases only work if the router actually
advertises them. They cannot identify an uncached HF repository.

`LLAMA_CACHE` remains `/models/llama.cpp/<configMaps entry name>`, backed by
the existing writable NVMe hostPath `/mnt/k8s-local-nvme/inference-models`.
Native caches survive pod replacement; each GPU has its own download copy.
The model-free router rediscovers cached models on startup. `--models-max 1`
limits loaded models per router and permits native LRU unloading when switching;
it is not a disk-cache quota. The chart now enforces one replica per deployment
(`inference.gpuCount=1`) to avoid four GPU requests and duplicate cache writers.
No storage, image, GPU allocation, or resource-limit migration was performed.

Migration and limits:

- Remove all `inference.modelSource` overrides, including `localPath`; schema
  validation rejects them instead of silently fixing a model at deployment.
  The former fixed local-file mode is intentionally removed. Existing native
  cached models remain usable offline. Arbitrary loose GGUF files need native
  directory/preset registration, not the removed fixed-model startup path.
- This contract selects native HF `owner/repo:quant` identifiers, not arbitrary
  URLs, revisions, filenames, `@32k` aliases, or non-GGUF transformers models.
  HF downloads follow `main`; they are not immutable revision/checksum pins.
  Native file/shard resolution is delegated entirely to llama.cpp.
- Verify NVMe capacity and writable permissions, HF HTTPS egress, model/runtime
  compatibility, P40 offload, VRAM and host-memory requirements before rollout.
  The retained 2Gi host-memory budget and 32768-token context are not suitable
  guarantees for every requested model. No automatic cache eviction is added.
- Native management endpoints can download/delete large files. Keep these
  single-router endpoints trusted/internal; no new management Ingress is added.
  Gated/private HF authentication is not configured by this change.

Evidence: pinned upstream [router documentation](https://github.com/ggml-org/llama.cpp/blob/339f3242c761f16c27e5ffcde1d35dcc31933a55/tools/server/README.md#using-multiple-models),
[router implementation](https://github.com/ggml-org/llama.cpp/blob/339f3242c761f16c27e5ffcde1d35dcc31933a55/tools/server/server-models.cpp)
(`ensure_model_ready`, request metadata lookup, `post_router_models`), and
[native router tests](https://github.com/ggml-org/llama.cpp/blob/339f3242c761f16c27e5ffcde1d35dcc31933a55/tools/server/tests/unit/test_router.py)
(`test_router_download_model`, download conflicts, incomplete downloads).
Local verification covers chart/schema rendering, worker mocked HTTP contracts,
download-before-chat ordering, external-provider compatibility, and lint.
No deployment, cluster mutation, secrets, weight download, or GPU runtime test
was performed. No standalone b11783 release binary or local llama-server was
available; host containerd was intentionally not used for validation.

## Verified Recovery: 2026-10-21

- Both inference Deployments were patched sequentially to `Recreate`, matching
  `helm/inference/templates/deployments.yaml`. Both reached generation 60,
  observed generation 60, one updated/Ready replica, and
  `Progressing=True / NewReplicaSetAvailable`. GPU requests and image references
  were unchanged. Both running images resolved to
  `ghcr.io/ggml-org/llama.cpp@sha256:31b39eacce2bab5fde9286ec3980ed5548a3987644e94da0d0c2092f93313538`.
- Live remediation used individual `kubectl patch deployment` operations and
  bounded `kubectl rollout status` checks, not a Helm release upgrade. The deploy
  script was not run: it upgrades both releases and deletes the seed worker Job.
- HTTPS verification for `code-factory.local` failed hostname validation. The
  referenced `orchestrator-gateway-tls` Secret is absent; no cert-manager CRDs,
  Traefik certificate resolver, or reusable ingress TLS certificate was found.
  The only TLS Secret found was the Kubernetes serving certificate, not an
  application certificate. Local hostname resolution also failed.
- Required TLS prerequisite: an approved, reachable DNS hostname and a valid
  certificate/chain trusted by both GitHub runners and internal workers, supplied
  through an approved certificate mechanism. GitHub reachability is unverified.
  No certificate bypass or self-signed trust rollout was used.
- Current Traefik pod IP is `10.42.0.46` with host networking disabled; this is
  topology evidence, not a captured gateway peer address. Proxy trust remains
  disabled with no trusted CIDRs. Before enabling it, confirm the immediate peer,
  restrict trust to its narrow address range, and validate namespace ingress
  isolation without disrupting external metrics scraping. No isolation policy
  was added while TLS and the final access path remain unresolved.
- The local gateway TTL implementation and chart setting are not deployed:
  running `/app/server.py` lacks `_task_job_ttl_seconds`, and
  `TASK_JOB_TTL_SECONDS_AFTER_FINISHED` is absent. CI publishes seven-character
  SHA tags and `latest`, and cannot include the uncommitted TTL changes. A
  rootless builder is available, but access to the K3s containerd socket is
  denied. Deployment needs explicit image-publication permission or an
  administrator-approved local import; neither was attempted.
- Gateway and supervisor remain Ready with their existing image references.
  Gateway runtime digest is
  `ghcr.io/utopiklab/code-factory-gateway@sha256:e972d35041ebe8a4111c225f890bb76b93b159a4195fc0b1e51dca5c841c7978`.
  The task PVC is Bound; `/data/tasks/tasks.sqlite3` does not yet exist. No task
  was submitted to initialize it. The seed Job remains suspended with no TTL;
  all seven historical Jobs and credential Secrets were preserved.
- Focused validation: 112 tests and 24 subtests passed across gateway and Helm
  registry tests; both charts passed `helm lint`; render assertions verified
  `Recreate`, unchanged GPU allocations, the 86400-second TTL env, and fail-closed
  proxy defaults. Older sections below describe the original deployment, not a
  current end-to-end health guarantee.

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
kubectl exec -n orchestrator -l app=orchestrator -c gateway -- \
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
- **JUDGE_MODEL:** qwen2.5-coder-7b-instruct-q4-km@32k

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
gateway deployment (port 8000)
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

> **Strategy:** Lightweight Prometheus **port-scraping** (Option 1). Each component
> (gateway, supervisor, worker) exposes a `/metrics` endpoint on **:9000** served
> by `prometheus_client` (Counter/Gauge/Histogram). No sidecars, no HTTP metrics, no
> Loki, and **no second Prometheus/Grafana stack** — metrics are scraped by the
> external parent Prometheus on TrueNAS via NodePort Services.

### Metrics Collected
- **Host metrics** (node-exporter → central Prometheus on TrueNAS)
  - CPU utilization per-core + aggregate
  - Memory (total/free/cache)
  - Disk usage + I/O
  - GPU utilization + VRAM (if exposed)

- **Pod metrics** (K8s metrics-server)
  - Per-pod CPU/memory
  - Container status

- **Application metrics** (port-scraping on :9000)
  - **Gateway** (`orchestrator/gateway`): `http_requests_total`,
    `http_request_duration_seconds`, `http_request_errors_total`,
    `gateway_active_requests_gauge`, `gateway_healthz_gauge`,
    `gateway_scrape_interval_seconds`, `gateway_tasks_accepted_total{kind}`
    (successful initial/retry intake), and `gateway_tasks_by_status` (current
    durable task rows grouped by `queued`, `running`, `completed`, or `failed`)
  - **Supervisor** (`orchestrator/supervisor`): `orchestrator_tasks_total`,
    `orchestrator_iterations_total`, `orchestrator_best_score`,
    `orchestrator_current_iteration`, `orchestrator_supervisor_state`,
    `orchestrator_duration_seconds`, `orchestrator_active_tasks`
  - **Worker** (`orchestrator/worker`): `worker_tasks_total`,
    `worker_code_changes_total`, `worker_job_duration_seconds`,
    `worker_last_status`, `worker_active_jobs`

### Central Prometheus
- **Endpoint:** http://truenas.utopiklab.lan:9090/  (Grafana + Prometheus)
- **Scrape target:** node-exporter on k8s-node01:9100 (host/pod metrics)
- **New scrape targets:**
  - `k8s-node01:30900` (`orchestrator-gateway-metrics`)
  - `k8s-node01:30901` (`orchestrator-supervisor-metrics`)
  - `k8s-node01:30902` (`orchestrator-worker-metrics`)

### Grafana Dashboards
- Add data source pointing to central Prometheus (http://truenas.utopiklab.lan:9090)
- Import node-exporter dashboard
- **Import** `dashboards/grafana-dashboard.json` (uid `cfe-orchestrator`) — combined
  pipeline dashboard: request/error rate + latency (gateway), tasks over time /
  current task state / current iteration / best score (supervisor), tasks by
  outcome / code changes / job duration / active jobs (worker), plus active-workload gauges.

### Known Issues
- iDRAC/IPMI exporter pod in CrashLoopBackOff (config format issue) — not critical for core loop

---

## How to Trigger a Task

### Option 1: GitHub Actions Workflow (automatic)
Push a branch that opens/assigns an issue on the repo:
```bash
git push origin feature/your-feature
# → GitHub Actions runs drain.yaml
# → POST to orchestrator gateway endpoint
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
    "repo": "UtopikLab/code-factory",
    "issue_number": 123,
    "issue_title": "Fix: broken test",
    "issue_url": "https://github.com/UtopikLab/code-factory/issues/123"
  }'
```

Expected response:
```json
{
  "status": "accepted",
  "task_id": "test-001",
  "repo": "UtopikLab/code-factory",
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
kubectl exec -n orchestrator <pod-id> -c gateway -- \
  python3 -c "import http.client; c=http.client.HTTPConnection('localhost',8000); \
  c.request('GET','/'); print(c.getresponse().read())"
```
Verify the gateway container is ready and port 8000 is bound.

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
- [`docs/thinktank-plan.md`](../design/thinktank-plan.md) — source-of-truth design
- [`docs/keep-vs-defer-architecture.md`](../design/keep-vs-defer-architecture.md) — repo implementation posture
- [`docs/build-plan.md`](../build/build-plan.md) — concrete build steps

---

**Deployed by:** Orchestrator Agent  
**Last updated:** 2026-10-11 23:52 UTC  
**System ready for autonomous operation.**
