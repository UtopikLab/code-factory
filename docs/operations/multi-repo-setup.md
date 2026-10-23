# Multi-Repository Setup

Repository authentication uses GitHub App installation tokens only. Read the
[setup and breaking rollout requirements](credential-separation.md) first.

For each source repository:

1. Install the App with selected-repository access (recommended) or all-repository
   access and independently verify its repository ID, owner ID, and installation
   ID. All-repository access broadens the installation's eligible repositories;
   probe and execution tokens remain scoped to one repository.
2. Add its policy to `githubAuth.repositories`, including exact trusted
   workflow/ref/subject/event bindings. Both issue and manual retry events need
   their own bindings. Repository names alone do not authorize a signed identity.
3. Install the canonical [drain workflow](../../.github/workflows/drain.yaml).
   It sets `CODEFACTORY_URL` to `https://code-factory.utopiklab.com` and
   `CODEFACTORY_OIDC_AUDIENCE` to `https://code-factory.utopiklab.com/tasks`
   directly; no Actions URL secret or audience variable is required.
   Verify that the gateway audience matches the literal workflow audience.
   The workflow requires `id-token: write`; it has no mode or PAT fallback.

Management lists the configured policies and submits repository identity using
its separate service token. No repository-to-PAT mapping exists. Human Basic/OIDC
login, supervisor internal trust, model credentials, and kubelet GHCR pulls remain
independent. Only the gateway receives the App private key.

Automatic Python source/test discovery remains available with `worker.repoPolicies: {}`.
These execution policies do not authorize repository access. Workers obtain
read-only, task-bound tokens from the broker; supervisor publication obtains
write-scoped tokens. Both renew before expiry, including delayed execution.

Keep the opaque task ID returned by intake: issue numbers collide across repos.
HTTP 202 means queued, not completed. Callback tokens are task-bound; Actions
retries are bound to the original repository. A duplicate submission cannot
replace credentials or restart work. Terminal cleanup and intentional retry
semantics are described in the credential guide.

Before an authorized rollout, rebuild/select all four orchestrator images, update
source workflows and policies, and drain or separately reconcile old PAT tasks.
Old credential artifacts cannot execute or silently become App-backed tasks.
Preserve GHCR and model credentials and existing catalog pinning. No live setup,
deployment, credential mutation, task migration, or remote edit is performed by
local validation. Verify live permissions, transport isolation, sandbox support,
and persistence separately. Use separate namespaces/gateways for different trust
domains; management status access is not tenant isolation.
