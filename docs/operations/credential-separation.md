# Credential Separation And Migration

This is a local implementation, not a deployment or a token rotation. Treat the
previously exposed classic PAT as compromised. Its owner must revoke/replace it
through a separately authorized process, audit its consumers and activity, and
replace each consumer with a credential for its actual purpose. Do not reuse it
for migration. The local real secret-values file is intentionally not modified.

## Three Independent Credentials

| Purpose | Source | Consumers |
|---|---|---|
| GHCR image pulls | Private Helm YAML or existing Kubernetes `imagePullSecrets` | Kubelet only; GHCR `read:packages` |
| Gateway authentication | Dedicated Secret key `gateway-auth-token` | Gateway and assigned workers; source workflow secret `ORCHESTRATOR_TOKEN` |
| Repository operations | Originating repository's `TASK_REPOSITORY_TOKEN` Actions secret | A single task's immutable Kubernetes Secret; assigned worker and terminal lifecycle sync |

Use an independently generated gateway secret, not a GitHub or registry token.
Existing image pins are untouched. No Azure Key Vault
or managed-identity integration is claimed by this Kubernetes implementation.

## Private Helm Values

Use `helm/secrets-values.yaml` like a local `.env` file, but with YAML syntax:

```sh
# Only on first setup; do not overwrite an existing private file.
test -e helm/secrets-values.yaml || (umask 077; cp helm/example.secrets-values.yaml helm/secrets-values.yaml)
chmod 600 helm/secrets-values.yaml
```

Edit that private file locally. Replace the compromised PAT with a newly issued
GHCR credential with **read:packages only**, never repository-operation grants.
Remove the misplaced `orchestrator.ghcr-secret` entry; it is not used. Set:

```yaml
registry:
   create: true
   username: "<your-github-username>"
   token: "<new-read-packages-only-token>"
gateway:
   auth:
      existingSecret: ""
      token: "<independent-random-gateway-token-at-least-32-characters>"
supervisor:
   config:
      openai-api-key: "<your-openai-key>"
```

Replace every placeholder before deployment. The gateway token must be independently
generated, at least 32 characters, and also configured as `ORCHESTRATOR_TOKEN` in
the source repositories. It is not the registry or repository token. Alternatively,
keep `gateway.auth.existingSecret: orchestrator-gateway-auth` and `token: ""` to
use a separately provisioned Secret with key `gateway-auth-token`.

When separately authorized, run `./helm/deploy.sh`. It passes the same private
YAML to both independent charts, without putting tokens in shell arguments.
`registry.create: true` creates a `kubernetes.io/dockerconfigjson` Secret named
`ghcr-secret` in each chart's configured namespace (`inference` and `orchestrator`
by default), and the workload Pods reference it. Both charts can also be installed
independently with `--values`. Partial, blank, and angle-bracket placeholder registry
credentials fail validation. Nonempty credentials with creation disabled also fail.

To retain manual pull Secrets, omit the registry block (the default is
`create: false` with empty credentials). Custom `imagePullSecrets` names remain
supported in that mode; creation requires a `ghcr-secret` reference. Do not enable
creation over an externally managed same-name Secret: Helm refuses ownership
conflicts rather than silently adopting it. Plan an explicit ownership transition.
Disabling creation later removes the Helm-managed Secret; provision its replacement
before doing so.

The real file is git-ignored and must never be committed or staged; the example
is safe to commit. Ignoring a file is not a vault. Helm stores supplied values and
rendered Secrets in release records, including the registry credential in both
namespaces. Restrict Helm release/Secret RBAC, backups and local file access, and
enable cluster encryption at rest. Do not use `helm get values`, rendered-manifest
dumps, `--debug`, or shell tracing with real credentials. This change does not
revoke/rotate any token, edit the existing private file, or deploy anything.

## Repository Permission Preflight

Initially use a fine-grained PAT limited to the originating repository, with
Contents, Pull requests, and Issues **read/write**, plus Metadata read. The
token's actor must have repository write access. Provision the `queued`, `done`,
and `failed` labels. GitHub Apps are future work, not implemented here.

Intake verifies canonical repository identity, an explicit `permissions.push`
value of true, non-archived state, authenticated contents/PR/issue reads, and the
same-repository issue identity before accepting work. Missing permission metadata,
read-only actor access, redirects, unauthorized reads, and inaccessible repositories
fail closed. Renames/redirects require the caller to use the canonical identity.

**Limitation:** repository metadata describes actor access, not every fine-grained
token grant. These safe read probes do not prove the token has every write scope,
nor bypass branch rules or organization policies. No throwaway write is performed
as preflight. Operators must configure the required grants; actual writes remain
subject to GitHub enforcement. A denied write becomes an explicit failed task or
`credential-denied` lifecycle state, not successful completion. Local mocks cannot
establish live permissions.

The drain workflow requires the dedicated repository secret; there is no implicit
`GITHUB_TOKEN` fallback. A manually adapted workflow using its built-in token must
account for expiration when its job ends (at most 24 hours), repository restrictions,
the repository setting allowing Actions to create PRs, and suppression of most
downstream workflow events from token-created PRs. It is unsuitable as the default
for work that outlives the submitting job. Expiry/revocation is not auto-refreshed.

## Configuration And Transport

Before a separately authorized rollout:

1. Provision a dedicated gateway-auth Secret through your secret manager. Set
   `gateway.auth.existingSecret` to its name; the key is `gateway-auth-token`.
   Alternatively set `existingSecret` empty and supply `gateway.auth.token` through
   your secure Helm mechanism. It must be at least 32 characters. Do not dump
   rendered Secret manifests, enable shell tracing, or put secrets in CLI arguments.
2. Set `gateway.ingress.host`, `gateway.ingress.tlsSecretName`, and
   `supervisor.config.acceptor-url` to a reachable HTTPS gateway origin with a
   trusted certificate. The ingress uses Traefik `websecure` and TLS. Workers must
   reach this HTTPS origin too; plain internal Service URLs are not accepted.
3. Behind that TLS terminator, explicitly enable `gateway.trustTlsProxy` and set
   `gateway.trustedTlsProxyCidrs` to the **actual immediate proxy source addresses**.
   Restrict gateway port 8000 to that proxy using cluster network policy/firewalls;
   ensure the proxy overwrites forwarded headers. Do not trust broad pod/client
   ranges or externally supplied forwarded headers. Defaults reject intake until
   this is configured. Only `ALLOW_LOCAL_HTTP=true` plus a loopback client/origin
   permits HTTP for local tests; the chart does not enable this opt-in.
4. Configure each source repository's three Actions secrets: `ORCHESTRATOR_URL`
   (HTTPS URL ending `/api/v1/tasks/spawn`), `ORCHESTRATOR_TOKEN` (gateway auth),
   and `TASK_REPOSITORY_TOKEN` (that repository's dedicated fine-grained PAT).
   Use the canonical [drain workflow](../../.github/workflows/drain.yaml).

Runtime `GITHUB_TOKEN` is not a fallback for gateway authentication or repository
work. Reconcile old in-flight tasks before switching versions; old DB rows are not
silently given a new global credential or callback identity.

All credential-bearing clients reject redirects. Repository credentials travel in
`X-Repository-Token`, separate from `Authorization: token <gateway-auth>`.
Structured payloads carry identity only; request headers are not logged. Do not
put credentials in issue text, repository files, URLs, patches, policies or payloads.
Known assigned secrets are rejected from model inputs and terminal results.
Worker-side filtering and task callback authentication are the primary report
boundary. The gateway's assigned-token comparison is best-effort during Secret API
outages so result durability does not depend on that API. GitHub-token-shaped values
are rejected independently of Secret availability. This is not universal secret
detection or protection against a malicious worker that already holds credentials.

## Task Ownership And Recovery

SQLite persists only the immutable Secret reference and callback hash, not the
repository credential or original callback token. The Secret holds the repository
credential, an independent callback token and an expiry timestamp. Worker Job env
entries use `secretKeyRef`, never literal credential values or serialized credentials.
The suspended seed Job is a template; only gateway-assigned Jobs execute. The
legacy supervisor direct-spawn path fails closed; its mock executor requires
explicit `SUPERVISOR_MOCK_MODE=true` and is not repository execution.

Duplicate intake revalidates access but cannot replace the assigned Secret, refresh
its expiry, change its callback, or redispatch an existing task. Repository identities
and issue numbers are bound to opaque task IDs. Reports require gateway auth **and**
`X-Task-Token` plus matching task/repository/issue identity. The shared gateway secret
still grants broad task read/intake/resync access: this is not tenant isolation.

Terminal results are immutable and saved before lifecycle sync. On transient GitHub
or Secret API failures, credentials remain available for retry. An authenticated
`POST /api/v1/tasks/{task_id}/sync` with JSON `{}` retries only the stored terminal
result, without a repository-token header or worker callback. It survives gateway
restarts; no background reconciler is implied. GET exposes `lifecycle` and
`credential_cleanup` (`retained`, `pending`, `deleted`). A successful sync is
`synced`; a deletion failure is `cleanup-pending`, retried without repeating writes.

Missing/expired/revoked/invalid credentials are explicit permanent lifecycle failures.
Unusable owned Secrets are deleted after terminal processing; deletion failures stay
visible as cleanup pending and can be retried with `/sync`. A mismatched Secret is
never adopted or deleted: operators must verify ownership before reconciling it.
No credential substitution or refresh endpoint exists. Keep the durable task result
and inspect branch/PR artifacts before separately authorizing a new attempt.

The default task credential TTL is 86400 seconds; `gateway.taskCredentialTtlSeconds`
allows 60 through 604800. It is an application use deadline, not automatic PAT
revocation or Kubernetes garbage collection. Queued/running tasks interrupted before
a terminal report require operator reconciliation; they retain their Secret until
that happens. Creation/dispatch failures persist terminal failure and attempt sync
and cleanup. An ambiguous crash while queued returns 503 on replay rather than
risking a second worker. There is no automatic Job watcher or reset.

Do not attach credential Secrets to Job ownerReferences: Job garbage collection
could otherwise delete a credential before lifecycle retries finish. Restore the
database and task Secrets consistently; callback hashes prevent adopting stale
same-name Secrets after a partial restore.

## Cluster Boundary And Local Evidence

Gateway RBAC can get only the named worker Job template, create Jobs, and
get/create/delete Secrets in its namespace. Kubernetes RBAC cannot restrict dynamic
Secret names by prefix, so use a dedicated orchestrator namespace with no unrelated
Secrets. Creating Jobs also lets that identity request pod Secret mounts; protect
the gateway accordingly. Workers and supervisor have service-account automount
disabled. Use cluster encryption at rest and restrict Secret access/backups; a
Kubernetes Secret is base64 storage, not encryption. This is not a claim of compliance
certification, workload tenant isolation, or protection against a cluster administrator.

The existing automatic Python policy discovery, bounded snapshot/model context,
network-isolated credential-free validation sandbox, and publication gates remain.
Tests exercise real httpx/OpenAI dependency paths with mocked transports and mocked
Kubernetes APIs, plus local Helm schema/render/RBAC checks. Live permissions, TLS/DNS,
proxy/network policy, container sandbox support, image rebuilds and PVC behavior
remain separately authorized deployment acceptance checks.