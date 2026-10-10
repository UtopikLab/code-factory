# LLM Inference on k8s-node01 (2x NVIDIA Tesla P40)

Bare-metal single-node K3s cluster inference guide, tuned to the **actual
hardware of `k8s-node01`** and not a generic "best practice" template.

```
Deploy:   ./inference/deploy.sh
Apply:    kubectl apply -f inference/
Endpoint: https://llm.local/v1/chat/completions   (OpenAI-compatible)
```

---

## TL;DR — the recommendation

| Decision | Choice | Why |
| :--- | :--- | :--- |
| **Inference engine** | **`llama.cpp` server** (OpenAI-compatible) | Only mainstream engine whose serving path does **not** depend on tensor cores — see §1. |
| **GPU scheduling** | NVIDIA standalone **device-plugin** DaemonSet | Lightest correct option on a single-node K3s/containerd host. |
| **Model format** | 4-bit **GGUF (Q4_K_M)** / **AWQ** / **GPTQ** | Quantization is the only way to both *fit* the weights and *mask* the bandwidth wall. |
| **Model size** | ≤ ~34B per card (23 GB VRAM at 4-bit); 70B across **both** cards | Fits VRAM; avoids the no-tensor-core penalty where possible. |
| **Weights storage** | PVC on `k8s-sc-ssd-replicated` (`/mnt/k8s-data-ssd`) | Replicated durability for expensive models. |
| **Exposure** | ClusterIP Service + `Ingress` (Traefik, K3s default) | Drop-in OpenAI client endpoint. |

---

## 1. Why `llama.cpp` and not vLLM / SGLang / TGI / Triton?

The single most important fact about this box is that the **Tesla P40 (Pascal,
GP102 datacenter cut) has NO tensor (CUDA) cores**. Modern high-throughput
inference engines are *built around* tensor cores, and this box cannot use them.

| Engine | What it optimizes for | On a P40 (no tensor cores, ~94 GB/s) | Verdict |
| :--- | :--- | :--- | :--- |
| **vLLM** | PagedAttention + FP16/BF16 tensor-core matmuls, CUDA graphs | The FP16 kernels fall back to **scalar FP16 emulation** — no tensor core to exploit. Speedup over a plain loop evaporates; BF16 is impossible (no tensor cores). | ❌ Poor fit. Technically runs int4 AWQ but gains little. |
| **SGLang** | Triton kernels + paged KV cache, tensor-core dependent | Same problem as vLLM; the fast path needs tensor cores. Its llama.cpp backend *is* basically llama.cpp. | ❌ Poor fit for the fast path. |
| **TGI** (HF) | flash-attention + tensor-core matmuls, FP16 | flash-attention needs tensor cores; on Pascal it degrades. Extra deps, no bandwidth win. | ❌ Poor fit. |
| **NVIDIA Triton Inference Server** | A flexible *runtime* that wraps an engine | Not a throughput engine itself — it just dispatches to vLLM/TensorRT/etc. On a P40 it offers no acceleration of its own. | ⚠️ Use only as a wrapper if you must reuse an engine; not a primary engine. |
| **llama.cpp** | Pure **kernel-based** math: int4 **GPTQ/AWQ** dequant + matmul, SIMD/vectorized loads | **Does not require tensor cores.** Its GPTQ/AWQ kernels are memory-bandwidth-optimized (vectorized, compressed loads) which partially *mitigates* the ~94 GB/s wall. Self-contained (no GPU-operator dependency beyond the device plugin). | ✅ Best fit for a Pascal card. |

**Bottom line:** on a GPU with no tensor cores and ~1/35th the bandwidth of an
A100, the engines that *need* tensor cores cannot deliver. `llama.cpp` is the
only mainstream server whose core matmul path is tensor-core independent, and
whose quantized kernels are the bandwidth-friendly path that actually helps on
these cards. It also exposes a first-class **OpenAI-compatible** API
(`/v1/chat/completions`, `/v1/models`, `/v1/completions`), so it can act as a
drop-in inference provider.

> **Caveat (be honest about throughput):** the P40 is memory-bandwidth bound.
> Even `llama.cpp` is slow here — see §2. Quantization is *required*, not
> optional: it both shrinks the model into 23 GB of VRAM *and* lets the
> bandwidth-optimized int4 kernels do more work per byte.

---

## 2. Performance reality on the P40

Effective bandwidth ≈ **94 GB/s** and it dominates every operation (the model
is read once per generated token). Rough per-token latency for a *streaming*
request (reading the whole active model once per token):

| Model | Active size @4-bit | ~Per-token latency | ~Throughput |
| :--- | :--- | :--- | :--- |
| 7B  (Q4_K_M)  | ~4 GB   | ~40 ms  | ~25 tok/s |
| 14B (AWQ Q4)  | ~9 GB   | ~95 ms  | ~10 tok/s |
| 32B (AWQ Q4)  | ~20 GB  | ~215 ms | ~5 tok/s  |
| 70B (Q4, both P40s) | ~35 GB split | ~100–140 ms* | ~7–10 tok/s* |

\*70B across both cards needs **cross-GPU all-reduce** per layer. The P40 is
**PCIe Gen3 x16 with no NVLink**, so cross-GPU synchronization is slow and adds
latency overhead; the gain is modest. **Recommendation:** run **one model per
GPU** (≤34B each) for the cleanest latency; use the combined 70B split only if
you specifically need a single large model.

**Best fit for this box:** low-latency, batched, or lightweight tasks —
classification, summarization, chat assistants, RAG, embeddings. **Not** a
high-throughput bulk-generation node.

---

## 3. Model strategy

### 3.1 Quantization is mandatory
- **GGUF** (`Q4_K_M`, `Q5_K_M`) — most universally compatible; any `llama.cpp`
  build loads it. Default recommendation.
- **AWQ** (4-bit) — newer, slightly better quality for instruction models;
  prefer the **`f8_e4m3`** KV cache (`tensor_type: f8_e4m3`) in the config for
  speed.
- **GPTQ** (4-bit) — good quality; requires the GPTQ/AWQ loader in `llama.cpp`.

### 3.2 VRAM budget (23 GB per P40)

| Precision | ~Params that fit one P40 | Notes |
| :--- | :--- | :--- |
| 4-bit (GGUF/AWQ/GPTQ) | **~30–34B** (≈20 GB) | Largest model that comfortably fits. |
| 8-bit | ~14–16B (≈13 GB) | Better quality, half the bandwidth savings. |
| FP16/BF16 | ~7–8B (≈14–15 GB) | **No tensor-core speed** on Pascal — slower than good 4-bit. Avoid for serving. |

> **70B does not fit one P40** at any practical precision. It only fits by
> splitting across **both** cards (46 GB combined) via llama.cpp multi-GPU.

### 3.3 Example default models (one per GPU)
- **GPU 0** (Bus-Id `0000:AF:00.0`): `Qwen2.5-14B-Instruct-AWQ` (~9 GB @4-bit)
- **GPU 1** (Bus-Id `0000:D8:00.0`): `Qwen2.5-7B-Instruct-AWQ` (~4 GB @4-bit)

Swap for any GGUF/AWQ/GPTQ checkpoint of the right size. See
[`inference/models/README.md`](inference/models/README.md) for where to put them
and how to download.

---

## 4. Deployment layout

```
inference/
├── README-inference.md          # this file
├── deploy.sh                    # install engine + apply manifests (containerd-aware)
├── common.sh                    # shared env (KUBECONFIG, containerd image pull)
├── nvidia-device-plugin.yaml    # GPU discovery on the node (namespace: gpu)
├── models-pvc.yaml              # weights storage  (storageClass: k8s-sc-ssd-replicated)
├── deployment.yaml              # llama.cpp server (2 pods, one GPU each) + Service + Ingress
└── models/
    └── README.md                # weight layout + download instructions
orchestrator/
├── namespace.yaml               # Namespace 'orchestrator'
├── supervisor.yaml              # always-on supervisor/judge Deployment + Service
├── workers.yaml                 # policy ConfigMap + coder-worker Job (spawn mechanism)
└── secrets.yaml                 # OpenAI key (never hardcoded)
monitoring/
├── namespace.yaml               # Namespace 'monitoring'
├── node-exporter.yaml           # host metrics DaemonSet → central Prometheus (TrueNAS)
├── idrac.yaml                   # iDRAC/PERC Redfish exporter + ConfigMap (transport param.)
└── secrets.yaml                 # iDRAC admin credentials (never hardcoded)
```

### 4.1 GPU scheduling (namespace `gpu`)
[`nvidia-device-plugin.yaml`](inference/nvidia-device-plugin.yaml) runs the
NVIDIA standalone **device-plugin** DaemonSet + node-problem-detector. It makes
the two P40s appear as `nvidia.com/gpu` resources so the scheduler can place the
server pods. No NVIDIA GPU Operator is required (it is heavier and unnecessary on
a single bare-metal node).

### 4.2 The server (namespace `llm`)
[`deployment.yaml`](inference/deployment.yaml) deploys two `llama.cpp` server
pods:

- **`llm-server-gpu0`** → pinned to Bus-Id `0000:AF:00.0` (`nvidia.com/gpu-device-id: "0"`), model from §3.3.
- **`llm-server-gpu1`** → pinned to Bus-Id `0000:D8:00.0` (`nvidia.com/gpu-device-id: "1"`), model from §3.3.

Each pod:
- requests & limits **`nvidia.com/gpu: 1`** and **`memory: 23Gi`** — we ask for a
  big chunk of VRAM because P40s idle at 0 %, but cap it at the card size so the
  scheduler never oversubscribes the node.
- reads its model from the weights PVC (`/models/checkpoints/`), loaded via the
  per-GPU CLI-argument **ConfigMap** mounted at `/etc/llama-server-args`.
- serves an **OpenAI-compatible** API on port `8000`.

Exposure: a ClusterIP `Service` + `Ingress` (Traefik, which K3s ships with)
publish `https://llm.local/v1/*`. See the Ingress in `deployment.yaml`.

### 4.3 Storage (consistent with the existing StorageClasses)
[`models-pvc.yaml`](inference/models-pvc.yaml) creates `llm-models` on
**`k8s-sc-ssd-replicated`** (`/mnt/k8s-data-ssd`, 1.9 TB xfs, the default class).
Weights are large and rarely mutated → replicated SSD is the right home. Do **not**
use `k8s-sc-nvme-fast` for multi-GB model weights (it is for small/fast local
PVs); NVMe is only worth it for the tiny metadata/log dirs.

---

## 4.4 Overnight agent stack (namespace `orchestrator`)

[`orchestrator/`](orchestrator/) is the always-on agent orchestration runtime.
It is a **supervisor / judge** that spawns **coder** worker pods on demand. It
sits alongside — and is independent of — the `llm` inference stack:

| Kind | File | Namespace | Role |
| :--- | :--- | :--- | :--- |
| `Namespace` | `orchestrator/namespace.yaml` | `orchestrator` | Isolates the agent runtime. |
| `Deployment` | `orchestrator/supervisor.yaml` | `orchestrator` | **Always-on** supervisor + judge (the agent loop). |
| `ConfigMap` | `orchestrator/workers.yaml` | `orchestrator` | Policy knobs (max-iterations, task-wait, worker count). |
| `Job` | `orchestrator/workers.yaml` | `orchestrator` | **Worker spawn** — N parallel coder agents per task. |
| `Secret` | `orchestrator/secrets.yaml` | `orchestrator` | OpenAI key + workflow-scoped GitHub PAT (never hardcoded). |

### 4.4.1 How the pieces relate to the inference stack

- **Supervisor (`orchestrator/supervisor.yaml`)** — an always-on Deployment that
  runs the LangGraph supervisor/judge loop. It is **CPU-only**: it talks to the
  judge and coder brains over the network (the `llm` servers), it does not hold
  a GPU. Resources are capped (~2-3 cores, 4-6 GiB) so it never starves the GPU
  servers.
  - The Deployment runs the loop in the background (resident, idle most of the
    time) and `exec`s a lightweight **task-acceptor sidecar** (`supervisor-task-acceptor.py`,
    ~800 lines) sharing the same pod and the same `:8000` port the Service
    routes to. The sidecar is a plain `ThreadingHTTPServer`; it has **no
    persistent HTTP surface** — it only binds `:8000` while a task is being
    accepted and rendered, then releases it (so it never competes with the
    resident loop for the port).
  - The sidecar's `do_POST` handles a single endpoint,
    **`/api/v1/tasks/spawn`**, gated by `Authorization: token <PAT>` (the
    workflow-scoped PAT from `orchestrator-secrets/github-token`). It validates
    the body `{name, ref, payload:{issue_number, issue_title, issue_url, repo,
    default_branch}}`, refuses any task naming a repo other than the one it was
    spawned for, renders `agent-job-template.yaml` (substituting `${IMAGE}`,
    `${TASK_ID}`, `${ISSUE_URL}`, `${REPO}`, `${GITHUB_TOKEN}`), creates the
    `Job`, and waits (`kubectl wait --for=jobcomplete`, 1800s) for it to finish
    before deleting the Job and returning `{uid, task_id, issue_number,
    status}`. `do_GET` returns `200` for any path (liveness).
- **Workers (`orchestrator/workers.yaml`)** — the **spawn mechanism**. The
  supervisor triggers a `Job` that runs N parallel **coder** agents (pure CPU,
  no GPU). `N` (the worker count) is a policy knob in the ConfigMap so it can be
  tuned without editing the manifest. This is how the overnight agent gets work
  done: the supervisor plans and judges, the workers execute.
- **The judge brain** — the supervisor's judge tool-calling agent is served by
  the **`llm-server-gpu1`** pod in the `llm` namespace (over `https://llm.local/
  v1`, OpenAI-compatible). The coder brain uses `llm-server-gpu0`.

### 4.4.2 Overnight workflow

1. The supervisor is always resident (idle, listening for a task).
2. A task arrives (webhook / cron / user). It is POSTed to the supervisor's
   task-acceptor at `https://<svc>/api/v1/tasks/spawn` with
   `Authorization: token <PAT>` and a JSON body naming the repo + issue.
3. The acceptor renders the Job template, creates the `Job`, and waits for it to
   complete (`kubectl wait --for=jobcomplete`).
4. The supervisor runs the LangGraph
   loop: plan → spawn coder workers → judge the result → retry / escalate,
   bounded by `max-iterations` (Open Question O5, default 8).
3. Workers execute code, emit output; the judge approves/rejects.
4. The supervisor opens a PR (or emits an event) and returns to idle.
5. Node-exporter + iDRAC telemetry report health to central Prometheus
   (TrueNAS), so overnight runs are monitored (§4.5).

### 4.4.3 Telemetry (namespace `monitoring`)

[`monitoring/`](monitoring/) reports hardware health to the **central
Prometheus on TrueNAS** (not a second Prometheus on the box):

| Kind | File | Namespace | Role |
| :--- | :--- | :--- | :--- |
| `Namespace` | `monitoring/node-exporter.yaml` | `monitoring` | Isolates the telemetry stack. |
| `DaemonSet` | `monitoring/node-exporter.yaml` | `monitoring` | Host CPU/RAM/GPU/disk metrics → Prometheus. |
| `DaemonSet` | `monitoring/idrac.yaml` | `monitoring` | iDRAC/PERC hardware health (Redfish or IPMI). |
| `ConfigMap` | `monitoring/idrac.yaml` | `monitoring` | iDRAC transport + target (parameterized). |
| `Secret` | `monitoring/secrets.yaml` | `monitoring` | iDRAC admin credentials (never hardcoded). |

- **node-exporter** — one DaemonSet process per node exposing `/proc` metrics on
  `:9100`. It does **not** run a separate Prometheus; it scrapes into the
  existing central Prometheus on TrueNAS (`http://truenas.utopiklab.lan:9090`).
  CPU/RAM limited (~1 core, ~1 GiB).
- **iDRAC exporter** — `grafana/redfish-exporter` reads the iDRAC/PERC
  (H730P/H740P) hardware health (temp, fan, power, SMART) via **Redfish
  Remote Write** (recommended for iDRAC 9.5+/10.5+) and forwards it to the same
  central Prometheus. The iDRAC **version → transport choice** (Redfish vs IPMI)
  is **parameterized** in the ConfigMap (Open Question O3), so the manifest is
  valid regardless of firmware.

### 4.4.4 Deploying the new namespaces

`inference/deploy.sh` now applies the orchestrator and monitoring stacks
alongside the inference stack (see §5.1). The new namespaces are:

- **`orchestrator`** — supervisor, workers, secrets.
- **`monitoring`** — node-exporter, iDRAC exporter, secrets.

---

## 5. Operating the stack

```bash
# Pull the device-plugin image into containerd and apply everything
./inference/deploy.sh

# GPU is now visible to the scheduler
kubectl get pods -A
kubectl get pvc -n llm -o wide

# Load a model into a server pod (weights already on the PVC):
kubectl exec -n llm deployment/llm-server-gpu0 -- \
  llama-quantize /models/checkpoints/qwen2.5-14b-instruct-awq.gguf \
    /models/checkpoints/qwen2.5-14b-instruct-awq/

# Talk to it (OpenAI-compatible)
curl -s https://llm.local/v1/chat/completions \
  -H 'Authorization: Bearer $OPENAI_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-14b-instruct-awq","messages":[{"role":"user","content":"ping"}]}'
```

---

## 6. Manifest quick-reference

| File | Kind(s) | Namespace | Key values |
| :--- | :--- | :--- | :--- |
| `nvidia-device-plugin.yaml` | DaemonSet, CRDs, RBAC | `gpu` | image `nvcr.io/nvidia/k8s-device-plugin:2.18.0` |
| `models-pvc.yaml` | PVC | `llm` | `k8s-sc-ssd-replicated`, 50Gi |
| `deployment.yaml` | ConfigMap ×2, Deployment ×2, Service ×1, Ingress | `llm` | `nvidia.com/gpu:1`, `memory:23Gi`, port 8000 |
| `orchestrator/namespace.yaml` | Namespace | `orchestrator` | |
| `orchestrator/supervisor.yaml` | Deployment, Service | `orchestrator` | CPU-only, limits cpu `2.5`/mem `5Gi`, port 8000 |
| `orchestrator/workers.yaml` | ConfigMap, Job | `orchestrator` | limits cpu `6.5`/mem `7Gi`, parallelism `1` |
| `orchestrator/secrets.yaml` | Secret | `orchestrator` | `orchestrator-secrets` (OpenAI key + GitHub PAT) |
| `monitoring/namespace.yaml` | Namespace | `monitoring` | |
| `monitoring/node-exporter.yaml` | Namespace, DaemonSet, Service, SA | `monitoring` | hostNetwork, limits cpu `1`/mem `1Gi`, :9100 |
| `monitoring/idrac.yaml` | ConfigMap, DaemonSet, Service, SA | `monitoring` | transport `redfish`, limits cpu `200m`/mem `256Mi` |
| `monitoring/secrets.yaml` | Secret | `monitoring` | `idrac-credentials` (iDRAC admin) |

---

## 7. Notes / gotchas

- **Driver/CUDA:** node has driver `580.178.04` (CUDA 13.0 runtime) but the
  `nvcc` toolchain is CUDA 12.4. The official `llama.cpp` images bundle CUDA 12
  libs, so they run against the system CUDA 12 libraries — verified compatible.
- **No MIG on P40:** MIG is barely useful on Pascal; allocate whole cards.
- **Single point of failure:** this is a single node; the two-pod design gives
  redundancy only if you tolerate the scheduler restarting one pod. For a
  multi-model host, keep one pod per GPU.
- **Re-quantize on the box:** if a downloaded GGUF/GPTQ doesn't load, use
  `llama-quantize` (see `inference/models/README.md`) to convert to Q4_K_M.
- **Scaling:** `replicas` is intentionally `1` per GPU pod — with exactly two
  GPUs on one node, two pods fully saturate the hardware with no oversubscription.
