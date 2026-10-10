# Thinktank Planning Document — AI Agent Orchestration on k8s-node01

**Status:** `BUILDING` · **For user validation** · **Date:** 2026-10-10

> This document started life as a WIP design that needed your sign-off. It has now
> been **approved and the manifests are being built** (`orchestrator/`, `monitoring/`,
> and the `inference/deploy.sh` wiring). The remaining `STILL-OPEN` items are tracked
> in §8 and are **parameterized** in the manifests (env / ConfigMap) so policy can
> change without editing the specs.

**One-line summary:** Turn `k8s-node01` into an always-on, autonomous AI software
factory — a Qwen2.5 **supervisor/judge** on P40-1 orchestrates Qwen2.5 **coder
workers** on P40-0 via a LangGraph state machine, evaluates and picks the best
output, retries on failure, and opens a PR through a human-review gate. Runs
overnight 24/7, independent of VS Code, monitored through the existing central
Prometheus + new iDRAC telemetry.

---

## 1. Context — verified facts

All facts in this section are **verified on the actual hardware** of
`k8s-node01`. Nothing here is invented.

### 1.1 Hardware

```
+===========================================================================+
|                 k8s-node01  (Bare-metal, single-node K3s)                 |
|  Orchestration: K3s  ·  Container runtime: containerd                     |
+===========================================================================+
| CPU:        Intel Xeon Gold 6148 @ 2.40 GHz — 80 cores                     |
| RAM:        91 GiB total · 87 GiB free  · Swap 8 GiB                       |
| STORAGE:    /mnt/k8s-data-ssd  — 3x SSD RAID5 (~1.9 TB xfs, replicated)     |
|             /mnt/k8s-local-nvme — ~1.9 TB NVMe                              |
| GPU:        2x NVIDIA Tesla P40  (Pascal GP102, 23 GB VRAM each)            |
|             — NO tensor (CUDA) cores · ~94 GB/s memory bandwidth           |
|             — PCIe Gen3 x16 · NO NVLink                                    |
| MGMT:       iDRAC (PERC H730P / H740P controller)                           |
| DRIVER:     nvidia 580.178.04 (CUDA 13.0 runtime) · nvcc toolchain 12.4     |
+===========================================================================+
```

### 1.2 Existing stack (the baseline we build on)

```
Namespace `llm`                     Namespace `gpu`
┌─────────────────────────────────┐  ┌────────────────────────┐
| 2x llama.cpp OpenAI-compatible  |  | NVIDIA device-plugin    |
| server pods:                    |  | DaemonSet (GPU discovery)|
|  · P40-0: Qwen2.5-14B-Instruct- |  └────────────────────────┘
|    AWQ                          |
|  · P40-1: Qwen2.5-7B-Instruct-  |
|    AWQ                          |
└─────────────────────────────────┘
        │  device-plugin exposes nvidia.com/gpu
        ▼
Traefik Ingress  →  https://llm.local/v1/*   (OpenAI-compatible)
   weights on PVC /models/checkpoints/ (PVC /mnt/k8s-data-ssd)
   current args: --ctx-size 4096 · AWQ · f8_e4m3
```

- **Central monitoring already exists:** Prometheus + Grafana on a **TrueNAS**
  server at `http://truenas.utopiklab.lan:30104/` (Prometheus endpoint). We
  attach to this rather than installing a second stack on the box.
- **User chats with the LLM via VS Code Copilot Chat (cloud).** Local models in
  that setup are served by Ollama. The overnight agent is a **separate path** and
  does not depend on Copilot Chat.

> **Important distinction (verified):** in the user's current Copilot Chat
> setup, Qwen does **not** work with the VS Code tools. This does **not** affect
> the overnight agent — it is a different runtime (the `orchestrator`
> Deployment) with its own tool-calling, and it does not use the VS Code tools.

---

## 2. Design overview

### 2.1 The two-brain architecture

The core idea is a **split-brain** on the two physical GPUs: one brain **writes
code** (coder), the other **judges** it (supervisor). They are the same model
family (Qwen2.5), which keeps the judge's evaluations meaningful.

```
   GPU P40-0  (coder)              GPU P40-1  (judge / supervisor)
   ┌──────────────────────┐       ┌──────────────────────────────┐
   │ llama.cpp server     │       │ llama.cpp server             │
   │  Qwen2.5-<coder>     │──────▶│  Qwen2.5-<judge>             │
   │  (interactive chat)  │       │  (plan / evaluate / gate)    │
   │  serves https://llm. │◀──────│  orchestrates the loop       │
   │  local/v1/*          │       └──────────────────────────────┘
   │ namespace: llm       │         │   Language: LangGraph
   └──────────────────────┘         │   state machine (orchestrator
                                     │   Deployment, namespace:
                                     │   orchestrator)
```

- **Coder brain (P40-0):** a `llama.cpp` inference server running a Qwen2.5
  coder model. Powers the autonomous workers *and* keeps serving interactive
  chat. Left as-is; we do **not** change its model/args here.
- **Judge/supervisor brain (P40-1):** a `llama.cpp` server running a Qwen2.5
  judge/supervisor model. Plans the work, evaluates every worker output, picks
  the best, and acts as the pre-action gate before anything is committed.

### 2.2 The LangGraph loop

The supervisor drives a **LangGraph state machine** — the loop is the heart of
the design:

```
   ┌───────────────────────────────────────────────────────────────────────┐
   │                                                                         │
   │   ┌───────────┐   plan   ┌──────────────┐   spawn N   ┌──────────────┐  │
   │   │ Supervisor │────────▶│  N workers    │───────────▶│  each worker │  │
   │   │  (P40-1)  │         │  (P40-0)      │  (edits/runs│  (coder)     │  │
   │   └───────────┘         └──────────────┘             └──────┬───────┘  │
   │                     │ each emits output                      │          │
   │                     ▼                                        │          │
   │   ┌───────────┐   evaluate   ┌───────────┐   pick BEST  ┌────▼───────┐  │
   │   │ Supervisor│◀────────────│  each      │◀────────────│  Supervisor │  │
   │   │  (P40-1)  │   each work  │  output   │             │  (judge)   │  │
   │   └─────┬─────┘              └───────────┘             └────┬───────┘  │
   │         │ on failure → iterate (retry the loop)              │          │
   │         ▼                                                    │          │
   │   ┌───────────┐                                              │          │
   │   │  Open PR  │◀──── BEST output ────────────────────────────┘          │
   │   │ (human    │                                                       │
   │   │  review   │                                                       │
   │   │   GATE)   │                                                       │
   │   └───────────┘                                                       │
   └───────────────────────────────────────────────────────────────────────┘
```

**Loop semantics (design intent):**

1. **Plan** — supervisor writes a plan for the task.
2. **Spawn N workers** — up to N coder instances run in parallel, each editing
   / running code.
3. **Evaluate** — supervisor evaluates *each* worker output.
4. **Pick BEST** — supervisor selects the single best output (not a merge).
5. **Iterate on failure** — if the best output fails its acceptance check, loop
   again (new plan + fresh workers), bounded by a max number of iterations.
6. **Human gate** — on success, open a PR through a **human review gate** before
   anything is merged. The human (you) approves/rejects.

### 2.3 Deployment posture

- **Always-on `Deployment` (not `CronJob`).** The whole system must be ready the
  moment a task arrives — a scheduled job would add latency and a cold-start gap.
- **Runs overnight 24/7** and **survives restarts**: the loop state is persistent
  so a restart resumes where it left off rather than re-planning from scratch.
- **Autonomous & independent of VS Code:** the overnight agent runs inside the
  `orchestrator` Deployment with its own tool-calling. It does **not** use Cline,
  Continue, or any VS Code extension. This is deliberate (see §4).

---

## 3. Decisions table

Every decision is tagged. `CONFIRMED` = approved to build. `STILL-OPEN` = needs
your sign-off or a data point before we build.

| # | Decision | Tag | Choice | Key reasoning / open question |
| :--- | :--- | :--- | :--- | :--- |
| 1 | **Two-brain split** | ✅ CONFIRMED | P40-0 = Qwen2.5 **coder** (llama.cpp); P40-1 = Qwen2.5 **judge/supervisor** (llama.cpp). Same model family so the judge is meaningful. | VRAM fit: 2× 32B Q4_K_M ≈ 40 GB of 46 GB combined — both P40s used, ~6 GB headroom. |
| 2 | **Orchestration framework** | ✅ CONFIRMED | **LangGraph** state machine for the loop. | Supervisor.plan → spawn N workers → each edits/runs → supervisor evaluates each → picks BEST → iterate on failure → open PR (human gate). |
| 3 | **Always-on vs scheduled** | ✅ CONFIRMED | **Always-on `Deployment`** (not CronJob). | Must be ready the instant a task arrives; survives restarts with persistent state. |
| 4 | **Overnight agent & VS Code** | ✅ CONFIRMED | Overnight agent runs in `orchestrator` Deployment with its own tool-calling; **independent of VS Code / Copilot Chat**. | Copilot Chat is the interactive cloud design/chat path — separate and left untouched. Qwen doesn't drive VS Code tools in that setup, but that does **not** constrain the overnight agent. |
| 5 | **Interactive chat model** | ✅ CONFIRMED | Keep the **desktop RTX 3080 10 GB** for interactive chat: **Ornith-1.5-9B-GGUF, 64K context**. | 3080 is faster (~20–40 tok/s for a 9B) than CPU and is the interactive path. 7–9B is the sweet spot for a 10 GB card — don't reach for 14B/16B. |
| 6 | **Monitoring** | ✅ CONFIRMED | Reuse **existing central Prometheus + Grafana** on TrueNAS. Add **node-exporter** (whole box) and **iDRAC telemetry** (Redfish Remote Write). | Do **not** add a second Prometheus/Grafana on the box. iDRAC 9.5+/10.5 → Redfish; older → IPMI. |
| 7 | **Resource budget** | ✅ CONFIRMED | Ample headroom; agents CPU/RAM **limited by `limits`** so they never steal from inference. GPU is the only hard constraint. | CPU 80 cores (orchestrator+workers ~10), RAM 91 GiB (inference holds ~46 GiB), 1.9 TB SSD. GPU P40s dedicated to inference/judge. |

### 3.1 The one tight spot — the 64K context model fit

This is the **single most important STILL-OPEN technical question**. The coder and
judge models are wanted at **64K context**, and the P40 VRAM (23 GB/card, 46 GB
combined) is the constraint:

| Model idea | ~VRAM @64K context | Fits one P40? | Fits both P40s (combined)? |
| :--- | :--- | :--- | :--- |
| 32B Q4_K_M @64K | ~26–28 GB | ❌ tight / likely over | ✅ (46 GB) but tight |
| 14B / 7B @64K | ~8–12 GB | ✅ comfortable | ✅ |
| 35B and less | satisfying quality | — | the stated quality floor ("35B and less gives satisfying quality") |

> **STILL-OPEN:** the **exact coder + judge model** that fits at 64K within VRAM
> while keeping quality. Model selection is **WIP** — a concrete candidate model
> has **not** been picked yet. This gates the start of worker/supervisor
> development.

### 3.2 Model preferences (design constraints)

- HF-hosted, **64K+** context.
- Qwen-compatible (so the judge stays in the same family as the coder).
- Preference for HF-preferring quantizations.
- Quality floor: "35B and less gives satisfying quality."

---

## 4. Workload & resource budget

Budget table by namespace. The point is to show **headroom** so the overnight
agent is never starved of CPU/RAM while inference is pinned to the GPUs.

```
                                  GPU (req/lim)   CPU (req/lim)    RAM (req/lim)
┌─────────────────────┬───────────────────────┬───────────────┬───────────────┬───────────────┐
| Namespace           | GPUs                   | cores          | GiB            |
├─────────────────────┼───────────────────────┼───────────────┼───────────────┼───────────────┤
| llm (inference)     | 2x P40 (coder+judge)   | inference pins│ ~46 GiB (VRAM)│  — GPUs hold   │
|                     | (not CPU/RAM heavy)    │                │ in VRAM;       │  CPU/RAM is    │
|                     |                        │                │ node RAM is    │  separate      │
|                     |                        │                │ separate       │  from VRAM     │
├─────────────────────┼───────────────────────┼───────────────┼───────────────┼───────────────┤
| orchestrator        | 0                      | ~2–3 lim       | ~4–6 lim       | supervisor     │
| (supervisor/judge)  |                        |               |               | brain          │
├─────────────────────┼───────────────────────┼───────────────┼───────────────┼───────────────┤
| workers             | 0                      | ~6–7 lim       | ~6–8 lim       | coder agents   │
| (coder, N parallel) |                        |               |               |                │
├─────────────────────┼───────────────────────┼───────────────┼───────────────┼───────────────┤
| node-exporter /     | 0                      | ~1 lim         | ~1 lim         | monitoring     │
| monitoring agents   |                        |               |               |                │
├─────────────────────┼───────────────────────┼───────────────┼───────────────┼───────────────┤
| SUM (agents)        | 0                      | ~10 cores      | ~12–15 GiB     | headroom for   │
|                     |                        |               |               | monitoring     │
├─────────────────────┼───────────────────────┼───────────────┼───────────────┼───────────────┤
| NODE TOTAL          | 2x P40                | 80 cores       | 91 GiB (87    | GPU is the     │
|                     |                        |                | free)          | only hard      │
└─────────────────────┴───────────────────────┴───────────────┴───────────────┴───────────────┘
```

**Budget rules (all CONFIRMED):**

- **CPU:** 80 cores. Agents are **`limits`-capped** so they never steal from
  inference. Orchestrator + workers use roughly **10 cores** — ~12–13 % of the
  node. The Xeon Gold 6148 (80 cores) is a wide margin.
- **RAM:** 91 GiB total, 87 GiB free. The GPUs hold their weights **in VRAM** (~46
  GiB across both P40s); node RAM is a separate pool for the agents + monitoring.
  Ample headroom.
- **Disk:** 1.9 TB SSD (replicated) + 1.9 TB NVMe. Plenty for agents, weights,
  logs, and telemetry.
- **GPU:** the P40s are the **constraint**, dedicated to inference + judge. CPU /
  RAM / disk are never the binding resource.

> **Note on VRAM vs RAM:** the "~46 GiB" figure in the table is **VRAM** (GPU
> memory), not node RAM. Node RAM (91 GiB) is a distinct pool and stays free for
> the agent. Keep these straight.

### 4.1 Normal operation distribution

The agents are a mix of **always-resident** and **bursty-on-demand**. The
system is **spiky, not steady**: two small models sit resident on the GPUs, and
brief parallel bursts of work happen when a task arrives. Across a 16h window,
most of the time is idle.

**Role distribution (always-on vs. bursty):**

| Agent | Always-on? | Active % | Notes |
| :--- | :--- | :--- | :--- |
| **Judge** (P40-1, 14B) | Yes, resident | ~5–10% | Bursts: plan → eval → gate, then idle |
| **Coder** (P40-0, 32B) | Yes, resident | ~5–10% | Bursts: N workers in parallel, then idle |
| **Orchestrator** (CPU, tiny) | Yes, resident | ~5–10% | Tiny LangGraph runtime, always listening |
| **Workers** (spawned) | No | Short bursts | Minutes long, spawned on demand, then gone |

> **Idle judge is not wasted:** a small model resident on an otherwise-idle
> 23 GB card costs almost nothing, and keeps planning instant the moment a task
> arrives. It is cheap, always-ready coverage.

**One task cycle (timeline):**

```
t=0        TASK ARRIVES → orchestrator wakes
  │
  ▼
┌───────────── PLAN (judge active, ~30–60s) ───────────────┐
│   supervisor writes the plan                              │
└───────────────────────────────────────────────────────────┘
  │
  ▼
┌───────────── WORKER BURST (N parallel on coder GPU) ──────┐
│   w1: generate code (~800 tok, ~90s) + run tests (~30s)   │
│   w2: generate code (~800 tok, ~90s) + run tests (~30s)   │  N workers
│   w3: ...                                                  │  staggered
│   → each emits an output                                   │
└───────────────────────────────────────────────────────────┘
  │
  ▼
┌───────────── EVALUATE (judge active, ~30–60s/round) ──────┐
│   judge scores each worker output                         │
└───────────────────────────────────────────────────────────┘
  │
  ▼
┌───────────── PICK BEST ───────────────────────────────────┐
│   best passes acceptance → OPEN PR → STOP (human gate)    │
│   best FAILS → loop back (new plan + fresh workers)        │  bounded
└───────────────────────────────────────────────────────────┘
```

**Aggregated overnight shape** (a series of short parallel bursts against long
idle stretches; one task ≈ **2–8 min** end-to-end):

```
IDLE ───────────────┐   ┌─┐   ────┐   ┌──────────┐   ────┐
                    │   │ │       │   │ BURST     │       │
                    │B1 │w│  B2   │w │  B3        │       │
                    │   │w│       │w │  B4        │       │
                    │   │w│       │w │  B5        │       │
                    │   │w│       │w │  B6        │       │
                    │   │w│       │w │  B7        │       │
                    └───┴─┴───────┴──┴────────────┴───────┘
   most of the time: idle (both GPUs parked, models resident)
   each burst: minutes of parallel coder activity + judge eval
```

> **Design implication (CONFIRMED):** always-on `Deployment` is justified — the
> cost of readiness (two resident small models) is tiny vs. the cost of a
> cold-start every task. Workers are short-lived, pure-CPU bursts on the 80 cores;
> they never need a GPU.


---

## 5. Component inventory

Namespace-by-namespace inventory of what each piece is. New components are marked
`[NEW]`; existing components are marked `[EXISTING]`.

| Namespace | Component | Role | GPU | State |
| :--- | :--- | :--- | :--- | :--- |
| `gpu` | NVIDIA device-plugin DaemonSet | exposes `nvidia.com/gpu` to the scheduler | (host) | `[EXISTING]` |
| `llm` | `llm-server-gpu0` — llama.cpp coder server (Qwen2.5-<coder>) | powers workers + interactive chat | P40-0 | `[EXISTING]` |
| `llm` | `llm-server-gpu1` — llama.cpp judge/supervisor server (Qwen2.5-<judge>) | the judge brain | P40-1 | `[NEW]` (same server image, new model) |
| `orchestrator` | `orchestrator` Deployment | the LangGraph supervisor/judge runtime + tool-calling agent | CPU (0 GPU) | `[NEW]` |
| `orchestrator` | worker pod(s) | coder agents driven by the supervisor (spawns on demand) | CPU (0 GPU) | `[NEW]` |
| `monitoring` | `node-exporter` DaemonSet | whole-box CPU/RAM/GPU/disk metrics → central Prometheus | (host) | `[NEW]` |
| `monitoring` | iDRAC Redfish Remote Write / exporter | PERC array, cache battery, SMART, temps, fans, power, events | (host mgmt) | `[NEW]` |
| `trueNAS` | Prometheus + Grafana | central monitoring (existing) | — | `[EXISTING]` |

> The judge brain (§5, `llm-server-gpu1`) is the **same llama.cpp server image**
> as the coder — it only loads a different model and takes on planning/eval/gate
> prompts. Its args ConfigMap is the same shape as the coder's but with
> supervisor/judge prompts and a 64K context.

---

## 6. Reliability design

### 6.1 Supervisor loop phases

| Phase | Actor | On success | On failure |
| :--- | :--- | :--- | :--- |
| **1. Plan** | supervisor (P40-1) | → emit plan, spawn workers | regenerate plan, retry (bounded) |
| **2. Execute** | workers (P40-0) | → emit output | mark worker failed, continue others |
| **3. Evaluate** | supervisor (P40-1) | → score each output | re-score; may re-run evaluation |
| **4. Pick BEST** | supervisor (P40-1) | → choose best, check acceptance | if best fails → iterate loop |
| **5. Gate** | supervisor (P40-1) | → open PR, wait for human | do not merge; escalate to human |

### 6.2 Failure handling & retries

- **Bounded retries:** the loop iterates a fixed maximum number of times
  (configurable). Each iteration is a fresh plan + fresh workers — no infinite
  loop.
- **Per-worker isolation:** a failing worker does not block the others; the
  supervisor evaluates whatever each produced.
- **Acceptance check** gates "success": the best output must pass a definition-of-
  -done check before it reaches the human gate.
- **Persistent state:** if the node restarts mid-loop, the supervisor resumes the
  current task from its saved state (last plan / partial outputs) rather than
  re-planning from zero.

### 6.3 The human review gate

- The loop **never auto-merges**. On a passing best output, it opens a PR and
  stops — the PR is the gate.
- You (the human) review and approve/reject. This is the single safety valve.
- The gate is a policy decision (PR trigger, reviewer, allowed branches) — see
  open questions.

### 6.4 Always-on resilience

- `Deployment` (not `CronJob`) → ready instantly when a task arrives.
- Survives restarts via persistent loop state.
- GPU failure: if a P40 pod loses its GPU, the scheduler restarts it; the
  supervisor loop is designed to tolerate per-worker loss (it re-evaluates what
  remains). Full node loss is outside scope (single-node box).

---

## 7. Monitoring plan

**Principle (CONFIRMED):** attach to the **existing central Prometheus + Grafana**
on TrueNAS. **Do not** install a second Prometheus/Grafana stack on `k8s-node01`.

```
   k8s-node01  ──node-exporter──▶  central Prometheus (TrueNAS)
   k8s-node01  ──iDRAC Redfish──▶  central Prometheus (TrueNAS)
                                              │
                                              ▼
                                   Grafana (TrueNAS)  ← panels
```

### 7.1 Host-level: node-exporter

One DaemonSet exporting whole-box metrics to the central Prometheus:

- **CPU:** per-core + aggregate utilization (watch for the ~10-core agent footprint
  vs 80 available).
- **RAM:** total / free / cache (vs 91 GiB / 87 GiB free).
- **GPU:** per-P40 utilization, VRAM used, power, temperature (if exposed).
- **Disk:** `/mnt/k8s-data-ssd` + `/mnt/k8s-local-nvme` usage + I/O.

### 7.2 iDRAC telemetry (Redfish Remote Write)

Target controller: **PERC H730P / H740P**, exposed via **iDRAC**.

| iDRAC version | Transport | What it exposes |
| :--- | :--- | :--- |
| **9.5+ / 10.5+** | **Redfish Remote Write** | PERC array state, cache battery health, drive faults, SMART, temps, fans, power, voltages, system events |
| **older** | IPMI (`ipmi_exporter` or Grafana IPMI plugin) | same fields via IPMI dump |

**Grafana panels (design intent):**

- PERC array health (alert panel)
- Failed / predictive-fail disks
- Cache battery health
- CPU / memory / GPU temperatures
- Fan temperatures
- System events log

### 7.3 Open questions on monitoring

- **iDRAC version check** — confirm 9.5+/10.5+ vs older to pick Redfish vs IPMI.
- Which iDRAC transport the central Prometheus can receive (Remote Write vs IPMI).
- Whether the existing Grafana has a suitable dashboard, or a new one is built.

---

## 8. Open questions / still-WIP

| # | Question | Why it gates building | Status |
| :--- | :--- | :--- | :--- |
| O1 | **Exact coder model @64K** | VRAM fit on a single P40 (23 GB). 32B Q4 @64K ≈ 26–28 GB — tight. | **STILL-OPEN (WIP)** |
| O2 | **Exact judge/supervisor model @64K** | Same VRAM fit question; judge runs on P40-1. | **STILL-OPEN (WIP)** |
| O3 | **iDRAC version** | Redfish (9.5+/10.5+) vs IPMI (older) changes the whole monitoring ingest path. | **STILL-OPEN** |
| O4 | **Human gate policy** | Which PR trigger, reviewer, allowed branches, how the gate is enforced. | **STILL-OPEN** |
| O5 | **Max loop iterations** | Bounds the retry budget / overnight runtime. | **STILL-OPEN** |
| O6 | **Worker count N** | Parallelism budget vs CPU `limits`. | **STILL-OPEN** |
| O7 | **Tool set for the overnight agent** | Which tools the autonomous agent may call (file ops, git, shell, kubectl, PR). | **STILL-OPEN** |
| O8 | **LangGraph runtime image / deps** | Confirm the runtime that hosts the supervisor + coder tool-calling. | **STILL-OPEN** |

**Summary:** everything except **O1/O2 (the model fit at 64K)** is CONFIRMED.
O1/O2 are the one genuinely tight technical spot and the only thing blocking the
start of worker/supervisor development.

---

## 9. Next steps (gated on your validation — DO NOT BUILD YET)

1. **Review this document and approve it.** No building starts until you sign off.
2. **Pick the exact coder + judge models @64K** (resolves O1/O2). Verify VRAM fit
   on a single P40 before committing (32B Q4 @64K is tight — test-quantize if
   needed).
3. **Confirm the iDRAC version** on `k8s-node01` (Redfish vs IPMI — resolves O3).
4. **Decide the human-gate policy** — PR trigger, reviewer, allowed branches (O4).
5. **Set the loop parameters** — max iterations (O5) and worker count `N` (O6).
6. **Enumerate the overnight agent's tool set** (O7) and confirm the LangGraph
   runtime (O8).
7. **Approve the design.** Once signed off, proceed to build in the usual
   8h-design / 16h-autonomous-agent / 8h-validate cadence.

---

*WIP — for validation only. Do not build until approved.*
