# Agent model + prompt + tools — the "each agent needs a spec" prompt

> A spec to implement before coding the supervisor loop. Each agent must have its
> **model**, **prompt**, and **tools** defined — the current code only defines the
> model matrix (partial) and leaves prompts and tools entirely implicit.

## The gap

The current design documents *which model* each role should use (see
`thinktank-plan.md` §2.4), but the supervisor code only carries a `worker-model`
string and the role tag. Three things are **missing**:

- **Model** — *partial.* The model matrix exists in the design doc; only the
  judge's model is present in Helm values. Worker roles have no per-role model.
- **Prompt** — *missing.* There is no role-specific instruction text. The
  `instruction` field in the spawn payload is absent.
- **Tools** — *missing.* There is no supervisor tool registry. The supervisor
  loop calls `worker.call()` but does not define *which tools* each worker may use
  (edit, validate, git/PR, etc.).

## The three implementation items

1. **Per-role model.** Add a model map to `values.yaml` (e.g.
   `workers.worker-models: { coder: "...", test: "...", ... }`) so each role
   resolves its own model instead of sharing one `worker-model`.
2. **Role-specific prompts.** Add a prompt map to `values.yaml` (e.g.
   `workers.worker-prompts: { coder: "...", test: "...", ... }`) and render it
   into the spawn payload so `build_role_payload` carries the right instruction.
3. **Supervisor tool registry.** Define a registry (e.g. `edit`, `validate`,
   `git`, `PR`) that declares, per role, which tools are available and their
   semantics, so the supervisor loop can bind tools to roles.
