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

Edit that private file locally. Use a separate provider-issued PAT for GHCR
`read:packages` and repository PR operations. Never reuse the GHCR PAT for
repository access. Internal gateway and supervisor tokens are generated
independently by the operator. This is the only supported chart credential
structure:
## Private Helm Values

Keep deployment behavior and credentials in separate files. Copy the tracked
nonsecret examples only when the corresponding ignored local file does not exist:

```sh
test -e helm/local.values.yaml || cp helm/example.values.yaml helm/local.values.yaml
test -e helm/orchestrator/local.values.yaml || cp helm/orchestrator/example.values.yaml helm/orchestrator/local.values.yaml
test -e helm/local.secrets-values.yaml || (umask 077; cp helm/example.secrets-values.yaml helm/local.secrets-values.yaml)
chmod 600 helm/local.secrets-values.yaml
```

`helm/local.values.yaml` contains shared, nonsecret settings for both charts,
including whether each chart creates the GHCR pull Secret and the registry
username. `helm/orchestrator/local.values.yaml` contains orchestrator-only
deployment settings such as images and environment routing. The
private `helm/local.secrets-values.yaml` contains the grouped
`credentials.internal.*`, `credentials.github.*`, and `credentials.models.*`
values, plus management repository credentials and admin or OIDC settings shown in
`helm/example.secrets-values.yaml`. Use a separate provider-issued
PAT for GHCR `read:packages` and repository PR operations; never reuse the GHCR
PAT for repository access. Generate internal tokens independently.

Replace placeholders with real values only in the ignored private credentials file.
The gateway token must be independently generated, at least 32 characters, and
also configured as `CODEFACTORY_AUTH_TOKEN` in source repositories. Helm always
creates and reconciles the purpose-separated authentication Secrets:
`orchestrator-gateway-auth`, `orchestrator-supervisor-service-auth`,
`orchestrator-github-pr-auth`, `orchestrator-coder-model-auth`, and
`orchestrator-openai-model-auth`. Names are derived from the chart name, not the
release name. Users supply only credential values, never auth Secret names or
creation switches. `credentials.internal.gatewayClientToken`,
`credentials.internal.supervisorServiceToken`, and `credentials.github.pullRequestPat`
are required; empty, whitespace, or placeholder values fail validation.
`credentials.models.openaiApiKey` and `credentials.models.coderApiKey` may be empty
for unauthenticated model endpoints. Their Secrets and keys still exist; configure
real provider-issued values whenever the endpoints require authentication.

### Management Credentials And Identity

When management is enabled, configure its credentials and identity in the same
ignored `helm/local.secrets-values.yaml`, at the root (not under `orchestrator`):

```yaml
management:
   admin:
      username: admin
      password: "<independent-admin-password-minimum-8-with-special-character>"
   repositories:
      owner/repository: "<repository-specific-pat>"
```

Replace the repository key and all placeholders with real values in that file.
Management defaults to `authMode: admin`, requiring no OIDC provider. Opening its
URL prompts for the admin username and password using browser HTTP Basic authentication.
The password must contain at least 8 characters, including a special (non-alphanumeric)
character, with no whitespace or angle brackets, and must be unique;
there is no built-in password. Helm creates `orchestrator-management-admin` and
passes the password through a Secret reference, never a ConfigMap. Use HTTPS at the
ingress or external TLS proxy and restrict direct backend access. This mode provides
one administrator, not user registration, password recovery, or browser logout;
close the browser session to clear cached Basic credentials. Public exposure also
requires proxy-level login rate limiting. No credentials are stored in browser storage.

To use an existing identity provider instead, explicitly set `management.authMode: oidc`
in the ordinary orchestrator values, and populate `management.oidc.issuer`, `audience`,
`jwksUrl`, and `allowedSubjects` in the private values file. Admin password authentication
is disabled in OIDC mode; invalid OIDC tokens never fall back to the admin account.

Repository configuration is optional: leave `management.repositories: {}` for local
admin operational access without any repository PAT. Admin authentication permits the
UI, session, configuration summary, and allowlisted task status summaries; these
responses do not expose repository content, patches, task payloads, or credentials.
Submitting repository work requires an explicitly configured repository PAT. The
gateway verifies that PAT with GitHub for the named repository's identity, read
permission, content, pull request, and issue access before accepting a task. Invalid
or insufficient PATs and unavailable validation fail closed, including for local
admins; admin credentials never substitute for repository authorization.
Blank configured repository tokens are rejected.
Missing OIDC fields or an empty authorized subject list are rejected only in OIDC mode.
Keep `management.enabled`,
the image, public origin, and ingress settings in the ordinary orchestrator values.
Helm creates `orchestrator-management-repositories` using the chart's
existing chart-name convention, with a `repositories.json` key containing the
repository-name-to-token JSON map, and mounts it into management automatically.
In OIDC mode, the OIDC fields become the management environment variables; authentication
and subject authorization remain required. The private file is already applied
last by `deploy.sh`; no script arguments or nested chart wrapper are needed.

There is no external repository-Secret override. When management is disabled,
its repository and admin Secrets are not rendered. The selected authentication
mode's settings are required only when management is enabled; a populated repository
map is required only to submit repository work.

The GHCR Secret is named `ghcr-read-packages-pull-secret` in each chart namespace,
and all workloads reference that name by default. The checked-in shared values
example enables chart creation and uses a username placeholder; set the actual
username in the ignored `helm/local.values.yaml`. Do not enable creation over an
externally managed same-name Secret: Helm refuses ownership conflicts rather than
adopting it. Disabling creation later removes the Helm-managed Secret, so provision
its replacement first.

This is a breaking values and resource-name change. Flat registry, gateway,
supervisor, and model-key aliases are rejected; there is no fallback or automatic
resource migration. The existing private file is not rewritten by setup. Migrate
its credentials locally into the grouped structure and move all nonsecret settings
to the appropriate ordinary values file; remove old flat keys and auth ownership
settings. Existing same-name Secrets that are not owned by this Helm release may
block an upgrade with an ownership conflict. Review ownership and arrange any
migration separately before an authorized upgrade; this change does not adopt,
delete, rename, or otherwise mutate cluster resources.

When separately authorized, `./helm/deploy.sh` applies chart defaults first,
`helm/local.values.yaml` to both charts, `helm/orchestrator/local.values.yaml` to
the orchestrator chart only, and `helm/local.secrets-values.yaml` last. This keeps
credentials out of behavior settings and shell arguments. For manual installs,
pass the same files in that order (omit the orchestrator override for inference).
Partial, blank, and angle-bracket placeholder registry credentials fail validation.
The real credentials file is git-ignored and must never be committed or staged.
Helm stores supplied values and rendered Secrets in release records, including the
registry credential in both namespaces. Restrict Helm release/Secret RBAC, backups
and local file access, and enable cluster encryption at rest. Do not use `helm get
values`, rendered-manifest dumps, `--debug`, or shell tracing with real credentials.
This change does not revoke/rotate any token, alter the existing private file, or
deploy anything.
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

1. Set `credentials.internal.gatewayClientToken` in
   `helm/local.secrets-values.yaml`. Helm creates the dedicated gateway-auth Secret
   with the key `gateway-auth-token`. The token must be at least 32 characters.
   Also supply the independent supervisor token and PR PAT in the same file.
   Do not dump
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
`code-factory.local`, keep that internal routing host even though the public
hostname differs. Without a rewrite, use the public hostname instead.
For example, with a public HTTPS origin and an internal Host rewrite:

```yaml
gateway:
   transportMode: edge-tls
    ingress:
         tlsTermination: external
         host: code-factory.local
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
During a separately authorized rollout, `./helm/deploy.sh` loads the shared
`helm/local.values.yaml` for both charts, then the orchestrator local override
for the orchestrator chart, then the private credentials file. Later files win.
Set `LOCAL_VALUES_FILE`, `ORCHESTRATOR_VALUES_FILE`, or `SECRETS_FILE` to select
different paths; keep custom local paths ignored too. For manual Helm commands,
pass the same applicable files in that order. Without an orchestrator override,
generic chart defaults retain internal TLS and external Secret management.

The filename prefix identifies the environment: chart `values.yaml` files hold
tracked defaults; `example.values.yaml` files are tracked generic examples;
`helm/local.values.yaml` is the ignored shared nonsecret override;
`helm/orchestrator/local.values.yaml` is the ignored orchestrator override; and
`helm/local.secrets-values.yaml` holds ignored credentials. The tracked credential
example remains `helm/example.secrets-values.yaml`. Future environments may use
`dev`, `staging`, or `prod` prefixes, but the script only defaults to `local`;
other paths require the explicit overrides above. Both charts receive the shared
local values and credentials; only the orchestrator receives its own local override.
Keep private files and environment-specific overrides ignored before using other
prefixes.

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
`Host: code-factory.local` returned HTTP 404 while the live Ingress used only
`websecure`. This supports the entrypoint mismatch; it is not authentication or
end-to-end TLS acceptance evidence. The local chart change does not itself
reconfigure the external reverse proxy, DNS, ingress, network isolation, or a running workload.

After correcting the public hostname on 2026-10-21, the local resolver returned
`70.53.188.185` via `wan1.utopiklab.com`. OpenSSL verified the certificate chain
and hostname for `code-factory.local`: issuer Let's Encrypt `YR1`, SAN
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