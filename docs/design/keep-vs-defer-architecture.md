# Repo-local minimal architecture note

Status: working repo guidance

This document is a repo-local implementation note. It does not replace the
source-of-truth design attachment and is intentionally more conservative: it
keeps the first build small, debuggable, and operationally realistic, while
explicitly deferring larger expansions until they are proven necessary.

## 1. Core position

The repo should optimize for a minimal viable loop first:

- one resident supervisor / planner
- short-lived coder workers
- one judge / reviewer
- human PR review gate

Everything beyond that is optional and should be introduced only when the loop
shows a real need.

This reduces failure surface, keeps GPU usage understandable, and avoids building
an oversized "AI factory" before the first loop has actually proven itself.

## 2. Keep vs defer

### Keep in the first build

- Supervisor / planner: owns task framing, iteration budget, and retry policy.
- Coder workers: run targeted edits, generate candidate patches, and execute
  focused validation.
- Judge / reviewer: compares results against acceptance criteria and selects the
  best output.
- Human review gate: every accepted patch becomes a PR; no auto-merge.

### Defer until evidence justifies it

- dedicated verifier agent
- separate PR-summary writer
- additional specialized roles
- broader model specialization by role
- large-context assumptions across all roles
- extra orchestration layers beyond the minimal loop

The design should remain open to future specialization, but the default posture is
"keep it simple and prove value first."

## 3. Runtime semantics

The system is better described as a 24/7 resident coding runtime than as a one-off
"overnight worker." It should be always available, but it does not need to be a
large multi-agent fleet. A resident supervisor with short-lived workers is enough
for the first milestone.

The runtime loop is:

1. receive task
2. supervisor plans the work and sets iteration budget
3. spawn coder workers
4. evaluate worker outputs
5. select the best candidate
6. retry within a bounded iteration cap if needed
7. open a PR when a candidate passes the human review gate

This is intentionally bounded and easy to debug.

## 4. Model strategy

Use a single, clear model family first instead of over-optimizing the model
matrix. The practical first choice is a Qwen2.5-Coder family with one stronger
planner and smaller worker models.

Recommended default:

- planner / supervisor: Qwen2.5-Coder-14B or a strong 7B/14B option depending on
  the loaded quantization and how much reasoning headroom is needed
- workers: Qwen2.5-Coder-7B-Instruct
- judge: same family as workers, or the same model used for the planner if the
  deployment is intentionally smaller

Keep the context window modest and justified by the task pattern. There is no
need to assume 64K for every role in the first build.

## 5. Operational posture

- CPU-only orchestrator: resident and lightweight
- GPU-backed inference: supports the planner and worker calls where needed
- workers: short-lived, spawned when tasks arrive, and destroyed after the task
  completes or fails
- monitoring: keep the runtime observable without introducing a second stack

## 6. When to expand

Expand only when the loop shows a consistent bottleneck, such as:

- repeated mis-evaluation by the judge
- too much validator churn for simple tasks
- a persistent need to separate planning, verification, and review into
  distinct roles
- clear evidence that the worker burst is too broad or too weak for the task mix

When that evidence appears, add one role at a time rather than broadening the
entire system at once.

## 7. Repo intent

This repo is an infrastructure and orchestration project first, not a sprawling
AI factory. The first credible milestone is a lean, always-on loop with clear
responsibility boundaries and a human approval gate.

That is the default operating model for the current repo.
