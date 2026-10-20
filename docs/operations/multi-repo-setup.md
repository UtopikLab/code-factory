# Multi-Repo Orchestrator Configuration

Each originating repository supplies its own task credential. A shared registry
token or global GitHub PAT is not a repository credential service.
Read [credential separation and migration](credential-separation.md) and the
[new repository execution contract](new-repo-setup.md) before rollout.

## Per-Repository Setup

Use the canonical [drain workflow](../../.github/workflows/drain.yaml) in each source
repository. Issue-only requests are supported; PR-only events are not implementation
requests. Configure these Actions secrets independently:

| Secret | Purpose |
|---|---|
| `ORCHESTRATOR_URL` | Trusted HTTPS task intake URL ending `/api/v1/tasks/spawn` |
| `ORCHESTRATOR_TOKEN` | Dedicated gateway-authentication secret, not a GitHub PAT |
| `TASK_REPOSITORY_TOKEN` | Fine-grained PAT scoped to this source repository |

The URL and gateway-auth secret may be shared among trusted callers. The repository
credential must not be shared across source repositories. It needs Contents, Pull
requests and Issues read/write, with Metadata read and an actor with repository write
access. The gateway checks identity, actor write metadata and authenticated reads
before acceptance. These checks do not prove every token write grant; GitHub still
enforces actual writes. Missing permission metadata is rejected, not guessed.

GHCR credentials remain only in existing Kubernetes `imagePullSecrets`, with
`read:packages`. They are not mounted into gateway/worker environments.
The workflow has no implicit built-in `GITHUB_TOKEN` fallback. The migration guide
explains workflow-token expiry, PR limitations and required rotation of the exposed PAT.

## Repository Selection

Set `gateway.config.repo` (or the fallback `supervisor.config.repo`) through the
operator-owned Helm configuration:

| Value | Meaning |
|---|---|
| `*` | Any repository identity, still requiring its own credential/access validation |
| `org/repo` | One allowed repository |
| `org/repo,other/project` | An additional comma-separated allowlist |

Automatic Python source/test discovery remains the default with
`worker.repoPolicies: {}`. Optional policy overrides narrow context, editable paths
and fixed validation commands. The sandbox and publication gates are unchanged.
An allowlist neither grants GitHub permissions nor creates a sandbox.

## Task Ownership And Recovery

Issue numbers collide across repositories. Keep the opaque task ID returned by
intake and use authenticated task GET state. A duplicate task cannot replace the
assigned credential, refresh its expiry or launch another worker. The task Secret
persists across gateway restarts and is used only by the assigned worker and terminal
lifecycle processing. Worker reports additionally require their task callback token.

HTTP 202 means dispatched, not completed. Completion requires a recorded terminal
result with a same-repository PR link and `lifecycle: synced`. Use authenticated
`POST /api/v1/tasks/{task_id}/sync` with JSON `{}` for terminal retry; never resubmit
another repository's credential. GET exposes lifecycle and credential cleanup state.
Expiry/revocation fails explicitly and does not select a global credential.

Transient failures retain credentials until retry. Owned Secrets are deleted after
sync or a permanent unusable-credential outcome; failed deletion can be retried.
Stale mismatched Secrets are not adopted or deleted automatically. Interrupted
queued/running tasks require operator reconciliation, not automatic replay/reset.

The gateway-auth secret still permits broad task read/intake/resync access. This
is not tenant isolation. Use separate gateways/namespaces for separate trust domains;
namespace-scoped dynamic Secret permissions cannot be limited by name prefix with RBAC.

## Rollout Boundary

Configure TLS, trusted immediate proxy source CIDRs, gateway network restrictions,
dedicated gateway auth and per-source credentials before separately authorized image
builds and deployment. Preserve registry credentials and select rebuilt image pins
explicitly. Never dump Secret manifests or send tokens in URLs, payloads, comments,
model context or diagnostic output. Local mocked tests do not establish live GitHub
access, sandbox support, TLS/network policy or PVC durability. No deployment, live task
submission or token rotation is performed by the local implementation checks.
