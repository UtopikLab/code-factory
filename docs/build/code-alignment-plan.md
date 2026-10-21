# Revised Code Alignment Plan

## Purpose and Boundary

This document records the implementation target and its verification criteria. Implementation was explicitly authorized for this alignment task; deployment and commits remain out of scope. The existing [build-plan.md](build-plan.md) is an older concrete runbook and is intentionally neither edited nor reconciled here.

## Architectural Recommendation

The primary risk is durable ownership and handoff: accepted work must survive process restarts, be consumed exactly once in effect, and retain unambiguous lifecycle ownership. Resolve the gateway-to-supervisor handoff and task contract before broad implementation. Do not let a comprehensive agent catalog become a prerequisite for demonstrating the first working workflow.

Target flow:

1. An external workflow authenticates with the gateway. The gateway validates and registers the request, creates a durable handoff, and exposes lifecycle/status only.
2. A CPU supervisor consumes the task, plans and decomposes it, chooses specialized roles, dispatches bounded Kubernetes Jobs, and collects their results.
3. CPU workers use the P40-0 coder endpoint and return candidate patches plus validation evidence. Workers do not judge or publish.
4. The supervisor uses the P40-1 judge endpoint to select candidates and decide whether to retry, within the approved limit of 8 iterations.
5. The supervisor owns publication of a selected candidate as a PR and the human review gate. It does not auto-merge.
6. The gateway exposes task lifecycle and status. HTTP `202` means accepted for processing, not completed.

Preserve the approved inference target on both P40s: **Qwen2.5-Coder-7B-Instruct-GGUF @ Q4_K_M, 32K context**. P40-0 serves coder requests; P40-1 serves supervisor planning, judgment, selection, and retry decisions.

## Configuration and Code Organization

Organize YAML and Python by domain: `gateway`, `supervisor`/orchestration, and `worker`. Keep monitoring configuration and resources separate from business and workflow configuration.

Package a centralized YAML agent-role catalog with the Helm chart. Give it a schema and stable role IDs, instructions, tool allowlists, input/output contracts, endpoint aliases, and bounded parameters. Keep executable planning, dispatch, API, edit, and PR logic in Python images. Helm values should provide deployment overrides, not duplicate the role catalog. Keep credentials and other secrets in Kubernetes Secrets; never put secret values in role specs or include them in config hashes.

Treat the catalog/config as a versioned contract:

- Validate it in CI and again at runtime. Include schema/spec versions; fail closed on unsupported versions, roles, or tools.
- Record the config version used by each task and Job. Mount the catalog read-only.
- Roll out changes using a config hash/version and pin in-flight Jobs to the version they started with, so a rollout cannot silently change active work.
- Hash only non-secret configuration. Keep secret rotation and references outside the catalog version/hash.

## Phases and Acceptance

### 1. Settle contracts and runtime

Agree on task identity, status transitions, handoff and credential ownership, idempotency, terminal states, and whether the limit of 8 means total attempts or retries after the first attempt. Choose the durable queue/outbox/poll mechanism and define exactly what `202` guarantees. Resolve O8 runtime selection, including dependencies and image ownership.

**Accept when:** the written API/lifecycle contract defines restart recovery, duplicate submission behavior, credential boundaries, and total iteration accounting; `202` has a precise, testable guarantee.

### 2. Establish catalog and schema

Define the agent catalog and schema, versioning, stable roles, tool/input/output constraints, endpoint aliases, and read-only/pinning rollout behavior. Keep observability separate from workflow settings.

**Accept when:** valid and invalid fixtures demonstrate schema validation; unknown versions/roles/tools fail closed; version/hash behavior and in-flight pinning are specified; secret data is excluded.

### 3. Implement gateway durable intake and status

Make the gateway authenticate, validate, durably register, and hand off tasks. Keep it out of worker scheduling and PR publication.

**Accept when:** the gateway never creates Jobs or PRs; acceptance is durable according to the agreed `202` contract; status accurately exposes asynchronous lifecycle; duplicate requests and restarts are safe and tested.

### 4. Implement supervisor consumption and dispatch

Have the CPU supervisor consume durable tasks, persist/recover workflow state, plan/decompose work, choose catalog roles, dispatch bounded Jobs, and collect candidate outputs. Route supervisor judge requests to P40-1.

**Accept when:** only the supervisor dispatches/selects/retries/publishes; restart recovery is idempotent; iteration accounting obeys the agreed 8-attempt/retry interpretation; in-flight Jobs use their pinned config version.

### 5. Refactor workers to candidate-only

Workers perform bounded role-specific edits, validate them, and return candidate patches plus evidence through the agreed result contract. Route code generation to P40-0.

**Accept when:** workers cannot judge, create branches/PRs, or otherwise publish; outputs include patch, role/task identity, and validation evidence; failed validation is reported as a candidate failure, not a publication decision.

### 6. Wire judgment, retries, PR, and lifecycle updates

The supervisor judges candidate patches and evidence through P40-1, selects or retries within the agreed cap, and owns PR publication and status updates through the gateway. Require human review; do not auto-merge.

**Accept when:** no worker publishes; supervisor alone selects/retries/publishes; PR credentials and policy are scoped as approved; terminal lifecycle reflects PR creation and human review state without claiming a merge.

### 7. Verify end to end and deployment configuration

Exercise the external workflow through gateway intake, restart/replay, supervisor dispatch, candidate collection, judge/retry, PR handoff, and status polling. Validate Helm rendering, schema/config agreement, endpoint routing, and monitoring separation.

**Accept when:** end-to-end tests cover async `202`, idempotency and restart recovery, the iteration cap, P40 routing, human review/no auto-merge, and correct terminal/status transitions; Helm render and config/schema checks pass; monitoring resources remain distinct from business/workflow config.

## Recommended First Milestone

The smallest low-risk milestone is the **config contract only**: valid/invalid catalog fixtures, schema/version validation, and a documented rollout/pinning design. Do this first only if it does not delay resolving durable handoff. If task, status, and credential contracts are already settled, prefer a thin vertical workflow through intake, durable handoff, supervisor consumption, one candidate-only worker, and status reporting; expand the catalog after that path proves the ownership boundaries.

## Code-Review Findings

The following are observations from reviewing current repository code, not fresh test results:

- The gateway creates Kubernetes worker Jobs in its intake path and reports `running` in the submission response, rather than acting only as durable intake/status with an explicit accepted contract: [server.py](../../orchestrator/gateway/server.py), [gateway.yaml](../../helm/orchestrator/templates/gateway.yaml).
- The supervisor’s runtime is an idle loop with optional synthetic autorun; worker execution is mock-only unless mock mode is enabled, and PR publication is a placeholder. Its runtime default is 2 iterations, which does not align with the approved limit of 8: [supervisor.py](../../orchestrator/supervisor/supervisor.py).
- The worker performs judge evaluation and GitHub branch/PR publication rather than returning a candidate-only result: [worker.py](../../orchestrator/worker/worker.py).
- The gateway submission response/status and completion callback requirements reflect a different workflow contract from the approved async `202 accepted, not completed` target: [server.py](../../orchestrator/gateway/server.py), [thinktank-plan.md](../design/thinktank-plan.md).
- Helm endpoint and configuration values are assembled across values and templates, while the schema and template expectations do not present one clearly aligned, versioned role/config contract. For example, the worker template derives endpoint settings and runtime defaults from supervisor config keys: [values.yaml](../../helm/orchestrator/values.yaml), [values.schema.json](../../helm/orchestrator/values.schema.json), [worker.yaml](../../helm/orchestrator/templates/worker.yaml), [supervisor.yaml](../../helm/orchestrator/templates/supervisor.yaml), [gateway.yaml](../../helm/orchestrator/templates/gateway.yaml).

No tests or render checks are claimed as run for this review.

## Implemented Contract

- The gateway stores accepted tasks in the shared SQLite database and returns `202 queued` only after the task row and task-scoped credentials are durable. Duplicate issue submissions return the original task; the gateway has no Job construction code or Job RBAC.
- The supervisor polls and atomically claims queued tasks, recovers expired claims without consuming another attempt or creating a different Job, persists a P40-1 plan, dispatches one bounded Job, judges candidate evidence, retries within eight total attempts, and owns PR publication and terminal status.
- Workers use P40-0 and return candidate patch, edits, base revision, role, and validation evidence. They have no judge implementation, global gateway token, or GitHub write operation; repository credentials with push permission are rejected.
- The gateway persists the Secret UID returned by creation and uses it as a deletion precondition without Secret-read permission. Conflicts, lost creation responses, and legacy rows without a UID retain the unverified Secret with `credential-binding-mismatch` and pending cleanup. An authorized operator must reconcile these Secrets; the gateway cannot automatically recover their binding or retry the task until cleanup is resolved.
- Native judge-model preparation runs in the supervisor before planning and evaluation. Its default follows the P40-1 router URL; the existing `worker.judgeNativeDownload` override is pinned in runtime configuration, not injected into workers.
- The supervisor reconstructs the candidate diff from the pinned base revision, checks it matches the worker patch, and judges that reconstructed diff. PR creation requires an explicit base-branch allowlist, reviewer list, and supervisor-only publication credential. Reviewers are requested; no merge API is called.
- The role catalog and schema are packaged read-only in a retained, content-addressed ConfigMap. Tasks pin the hash of catalog, schema, and non-secret runtime config; secrets and secret references are excluded from the hash. Unsupported versions, catalog/tool violations, or unavailable pinned ConfigMaps fail closed.
- The chart defaults to a single `ReadWriteOnce` SQLite volume and co-locates gateway and supervisor pods for shared local-filesystem locking. Default repository access is deny-all; operators must configure explicit repositories, PR policy, and credential values before tasks can run. Helm owns the authentication Secrets.

## Remaining Deployment Checks

- Confirm the selected cluster storage class permits both pods to mount the same `ReadWriteOnce` claim on one node and supports SQLite locking/durability semantics. This chart has not been installed or exercised against the cluster.
- Set `credentials.internal.supervisorServiceToken`, `credentials.github.pullRequestPat`, and any required model API keys in `helm/local.secrets-values.yaml`. Helm creates separate supervisor-service, GitHub-PR, and model-auth Secrets; use least-privilege GitHub scopes and confirm the task repository token supplied by workflows is read-only.
- Configure per-repository credentials, allowed base branches, reviewers, and the existing P40 endpoints/model IDs. Empty/default PR policy intentionally prevents publication.
- Validate actual worker Job lifecycle/status across supervisor restart, the selected GPU router compatibility with JSON response formats, reviewer policy availability, and GitHub branch/PR permissions in a non-production environment.

No deployment or external infrastructure provisioning is claimed by this implementation.
