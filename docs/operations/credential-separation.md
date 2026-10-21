# Credential Separation And Migration

This is a local implementation, not a deployment or a token rotation. Treat the
previously exposed classic PAT as compromised. Its owner must revoke/replace it
through a separately authorized process, audit its consumers and activity, and
replace each consumer with a credential for its actual purpose. Do not reuse it
for migration. The local private file is renamed without changing its contents.

## Three Independent Credentials

| Purpose | Source | Consumers |
|---|---|---|
| GHCR image pulls | Private Helm YAML or existing Kubernetes `imagePullSecrets` | Kubelet only; GHCR `read:packages` |
| Gateway authentication | Dedicated Secret key `gateway-auth-token` | Gateway and assigned workers; source workflow secret `CODEFACTORY_AUTH_TOKEN` |
| Repository operations | Originating repository's `REPO_GITHUB_PAT` Actions secret | A single task's immutable Kubernetes Secret; assigned worker and terminal lifecycle sync |

Use an independently generated gateway secret, not a GitHub or registry token.
Existing image pins are untouched. No Azure Key Vault
or managed-identity integration is claimed by this Kubernetes implementation.

## Private Helm Values

Use `helm/local.secrets-values.yaml` like a local `.env` file, but with YAML syntax:

```sh
# Only on first setup; do not overwrite an existing private file.
test -e helm/local.secrets-values.yaml || (umask 077; cp helm/example.secrets-values.yaml helm/local.secrets-values.yaml)
chmod 600 helm/local.secrets-values.yaml
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
generated, at least 32 characters, and also configured as `CODEFACTORY_AUTH_TOKEN` in
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
revoke/rotate any token, change the private file's contents, or deploy anything.

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
2. Choose `gateway.ingress.tlsTermination`: `internal` (default) uses Traefik
   `websecure` and requires a nonempty `gateway.ingress.tlsSecretName` for the
   routing host. `external` uses Traefik `web` over HTTP, without a TLS annotation
   or TLS Secret reference; no in-cluster TLS Secret is required in this mode.
   Set `gateway.ingress.host` to the Host received by Traefik, including any
   deliberate upstream rewrite. Independently set `supervisor.config.acceptor-url`
   to the public HTTPS origin with a trusted certificate. Workers must resolve
   and reach that origin; plain internal Service URLs are not accepted.
3. Set `gateway.transportMode: edge-tls` (the chart default). TLS is enforced by
   deployment infrastructure, not inferred by the application from proxy IPs or
   forwarded headers. Restrict gateway port 8000 and direct Service/NodePort paths
   using enforced NetworkPolicies/firewalls so credentials cannot bypass the TLS
   edge. Chart selection alone does not install or verify that isolation. Gateway
   authentication, per-task credentials and callback validation remain mandatory.
4. Configure each source repository's three Actions secrets: `CODEFACTORY_URL`
   (HTTPS base URL, without an endpoint path), `CODEFACTORY_AUTH_TOKEN` (gateway auth),
   and `REPO_GITHUB_PAT` (that repository's dedicated fine-grained PAT).
   Use the canonical [drain workflow](../../.github/workflows/drain.yaml).

### External Reverse Proxy TLS Route

The application supports two explicit `GATEWAY_TRANSPORT_MODE` values:

- `edge-tls`: the gateway serves internal HTTP; an ingress, load balancer or external
   reverse proxy enforces HTTPS and isolates the backend. No particular vendor,
   proxy IP or `X-Forwarded-Proto` header is required or trusted by the application.
- `local-http`: local development only; the server binds `127.0.0.1:8000` and
   accepts intake only from loopback. The Kubernetes chart does not permit this mode.

Unset or unknown modes fail startup. Direct gateway TLS termination is not
implemented; deployments without an external reverse proxy can terminate TLS
at their ingress instead. `gateway.ingress.tlsTermination: internal` describes
TLS at the ingress, not at the Python server. Worker and drain clients still
require verified HTTPS. Worker loopback tests use their separate
`ALLOW_LOCAL_HTTP=true` opt-in; it does not configure gateway transport.

The optional external TLS route is: external TLS reverse proxy -> HTTP ingress
-> gateway. The reverse proxy is equipment outside this project's scope; the
project does not manage its configuration. Set the Ingress host to the Host
received by Traefik. If the external proxy deliberately rewrites Host to
`orchestrator.local`, keep that internal routing host even though the public
hostname differs. Without a rewrite, use the public hostname instead.
For example, with a public HTTPS origin and an internal Host rewrite:

```yaml
gateway:
   transportMode: edge-tls
    ingress:
         tlsTermination: external
         host: orchestrator.local
         tlsSecretName: ""
supervisor:
    config:
      acceptor-url: https://gateway.example.com
```

Keep environment-specific routing in an ignored local override, not in chart
defaults or the committed [external proxy example](../../helm/orchestrator/example.values.yaml).
On first setup only (preserves an existing operator file):

```sh
test -e helm/orchestrator/local.values.yaml || cp helm/orchestrator/example.values.yaml helm/orchestrator/local.values.yaml
git check-ignore helm/orchestrator/local.values.yaml
```

Edit the local file's public HTTPS origin and routing host for your environment.
The example uses `gateway.example.com`; it is never automatically loaded.
During a separately authorized rollout, `./helm/deploy.sh` automatically loads
the local override if present, for the orchestrator chart only. Precedence is
chart `values.yaml`, then `helm/orchestrator/local.values.yaml`, then private
`helm/local.secrets-values.yaml`
(last wins). Set `ORCHESTRATOR_VALUES_FILE` to use a different local path;
`SECRETS_FILE` still selects the private file. Keep custom paths ignored too.
For manual Helm commands, pass the local override with `--values` before the
private values file. Without an override, generic chart defaults retain internal TLS.

The filename prefix identifies the environment: `values.yaml` holds shared,
tracked defaults; `example.values.yaml` is a tracked generic example;
`local.values.yaml` is an ignored environment override; and
`local.secrets-values.yaml` holds ignored credentials. The tracked credential
example remains `helm/example.secrets-values.yaml`. Future environments may use
`dev`, `staging`, or `prod` prefixes, but the script only defaults to `local`;
other paths require the explicit overrides above. Inference receives the shared
private file, never the orchestrator's local override. Keep private files and
environment-specific overrides ignored before using other prefixes.

`.gitignore` does not protect files already tracked by Git. If this local override
was previously tracked, run `git rm --cached -- helm/orchestrator/local.values.yaml`
to stage its removal from version control while keeping the file on disk. Review
that staged change before committing. Ignoring or untracking does not remove old
values from commit history. Do not force-add local overrides.

These are routing values, not a complete secure deployment configuration. They
do not restrict backend network access. Keep Traefik's `web` entrypoint free of a global
redirect to `websecure` for this route; credential-bearing clients reject
redirects. Do not change the Ingress host to the public hostname while the proxy
rewrites Host. Configure source workflows' `CODEFACTORY_URL` as
the public HTTPS origin, for example `https://gateway.example.com`.
An optional trailing slash is accepted; the workflow appends `/api/v1/tasks/spawn`.

Before deploying external mode, verify all of the following:

- Obtain the external proxy frontend VIP/IP for DNS and certificate checks.
   DNS must resolve the public
   name to the frontend from workers and submitters. Verify the frontend serves
   a certificate valid for the configured public hostname, including its chain in
   those clients' trust stores. Do not bypass verification or invent a certificate.
- Ensure the edge accepts credential-bearing requests only over HTTPS. HTTP
   entrypoints must not forward task submissions to the backend. The Host rewrite
   is a routing choice, not evidence of secure transport.
- Restrict edge-to-ingress HTTP access to the edge and restrict gateway access to
   the ingress using enforced network policy/firewall rules. Account for metrics
   scraping and health probes, which may need separate paths. Verify both cluster
   and LAN bypass attempts are denied before credential-bearing use. The gateway
   does not itself detect or prevent a misconfigured network boundary in edge mode.
- If other infrastructure components use forwarded headers, configure their trust
   separately. Forwarded headers do not authorize gateway intake or establish TLS
   inside the application.

On 2026-10-21, a credential-free GET to `http://192.168.1.51/api/v1` with
`Host: orchestrator.local` returned HTTP 404 while the live Ingress used only
`websecure`. This supports the entrypoint mismatch; it is not authentication or
end-to-end TLS acceptance evidence. The local chart change does not itself
reconfigure the external reverse proxy, DNS, ingress, network isolation, or a running workload.

After correcting the public hostname on 2026-10-21, the local resolver returned
`70.53.188.185` via `wan1.utopiklab.com`. OpenSSL verified the certificate chain
and hostname for `orchestrator.utopiklab.com`: issuer Let's Encrypt `YR1`, SAN
`*.utopiklab.com`, valid 2026-10-12 through 2027-01-10. A credential-free HTTPS
GET to `/healthz` returned HTTP 404 with successful certificate verification and
no redirect. The Ingress only routes `/api/v1`, so this response does not establish
an API routing failure or successful authentication. No worker pod was running;
worker-origin DNS/TLS reachability remains unverified. The public DNS address is
not evidence of the external proxy's backend source IP or the gateway's immediate peer.

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

### Explicit Task Retry

Ordinary intake replay still returns 409 for a failed task; reopening an issue or
re-running its original event does not erase that failure. After fixing the cause
and checking for any existing branch/PR artifacts, use the source repository's
**Actions -> Autonomous drain loop -> Run workflow** with the failed `retry_task_id`.
The workflow sends the gateway authentication and current repository credential to:

```text
POST /api/v1/tasks/{failed_task_id}/retry
{"retry_key":"operator-request-1"}
```

For manual API callers, choose a unique retry key for the intentional request and
reuse it when repeating that request. The workflow uses its run ID, which remains
stable across reruns of the same run. Requests require both normal intake headers;
the parent callback token is not reused. Access to the parent's repository is
revalidated. The task must be failed, and lifecycle processing must be settled
with credential cleanup complete. Pending sync/cleanup must be resolved via `/sync`
first. Permanent credential failures with completed cleanup may be retried with
a fresh repository credential; they do not require reviving the expired token.

A retry creates a new task ID, Secret, callback and one-worker Job. The old task,
result, Job, and logs are preserved. GET exposes `retry_of` and `retry_task_id` to
navigate attempt history. Only one direct retry is allowed per failed attempt;
concurrent or repeated requests cannot launch duplicate workers. Repeating the
same retry key returns that attempt's current state; a different key against an
already-retried parent returns 409 and its child ID. If the child fails, explicitly
retry that child rather than its ancestor. Running and completed tasks cannot be
restarted. Queued/ambiguous dispatches still require operator reconciliation.

This route does not fix missing models, dependency failures, or sandbox setup.
Rebuild/deploy the gateway image and update the source repository's workflow before
using it. Local tests mock all GitHub, Kubernetes, model and validation interactions;
they do not submit a live retry or prove a live PR outcome.

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

## Finished Task Job Retention

New gateway-created task Jobs set `spec.ttlSecondsAfterFinished` to 86400
seconds (24 hours) by default. Configure retention in orchestrator Helm values:

```yaml
gateway:
   taskJobTtlSecondsAfterFinished: 86400
```

The setting accepts integers from 1 through 604800 seconds (seven days) and is
passed to the gateway as `TASK_JOB_TTL_SECONDS_AFTER_FINISHED`. Invalid runtime
values fail Job creation rather than silently disabling cleanup. The suspended
worker seed remains a template with no TTL; assigned Jobs receive the TTL directly.

Kubernetes TTL-after-finished cleanup makes a Job eligible for cascading deletion
after it reaches `Complete` or `Failed` and the retention interval elapses. Cleanup
is asynchronous and removes dependent Pods and their Kubernetes-accessible logs;
export logs elsewhere for longer retention. This is not a running-task deadline.
The gateway's SQLite task records remain on its durable PVC. Credential expiry,
terminal processing and Secret cleanup remain independent; Secrets are not owned
by Jobs, so lifecycle retries can continue after Job cleanup.

Activation requires rebuilding the gateway image and a separately authorized Helm
deployment. No historical Jobs are changed or backfilled. Adding a TTL to an old
finished Job can make it immediately eligible for deletion, including its Pods
and logs; any such migration requires separate review and authorization. Local
render and mocked submission tests do not establish live TTL-controller behavior.

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