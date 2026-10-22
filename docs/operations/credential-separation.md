# Repository Authentication And Credential Separation

GitHub App installation tokens are the only supported repository credentials.
GitHub Actions submits work using OIDC; management submits policy-approved
repository identities using its distinct service token. There is no auth-mode
switch, source PAT intake, shared workflow gateway token, or credential fallback.

This is a local implementation change, not a deployment or credential rotation.
Treat any previously exposed PAT as compromised: its owner must revoke/replace it
and audit consumers through a separately authorized process. No private values
file is read or rewritten by this change.

## Operator Setup

Before a separately authorized rollout:

1. Create a GitHub App with Contents and Pull requests read/write, Issues read,
   and implicit Metadata read. Install it with **Only select repositories** for
   each trusted owner. All-repository or suspended installations are rejected.
   Organization approvals, branch rules, and human PR review still apply.
2. Provision its PEM in an externally managed Kubernetes Secret in the
   orchestrator namespace. Configure only the Secret name/key in values. Only
   the gateway mounts the private key; management, supervisor, workers, task
   records, and catalogs must never receive it. Restrict Secret/RBAC access and
   backups, namespace workload creation, and enable encryption at rest.
3. Independently verify the immutable App, repository, owner, and installation
   IDs. Configure exact trusted workflow/ref/subject/event bindings in
   `githubAuth.repositories`. Protect those branches and workflow files.
4. If management is enabled, provision a separate random service-token Secret,
   at least 32 characters without whitespace, distinct from the supervisor token.
   Only gateway and management reference it. Preserve human Basic/OIDC login;
   neither login method grants arbitrary repository access.
5. In each source repository set the Actions secret `CODEFACTORY_URL` to the
   trusted HTTPS gateway base URL and variable `CODEFACTORY_OIDC_AUDIENCE` to
   exactly `githubAuth.audience`. Install the canonical
   [drain workflow](../../.github/workflows/drain.yaml) through a separately
   authorized remote change. It always requests OIDC with `id-token: write`.

Use the complete nonsecret [orchestrator example](../../helm/orchestrator/example.values.yaml),
replacing its illustrative IDs, repository, audience, and Secret references.
App setup is mandatory even when management is disabled. Its service-token
reference is required only when management is enabled. Missing or unknown auth
settings fail closed; blank defaults intentionally cannot deploy.

## Breaking Rollout

Rebuild and explicitly select compatible **gateway, supervisor, worker, and
management** images together before an authorized rollout. Existing image pins
are intentionally untouched. Install the updated source workflow and policy
bindings together; an old client will not authenticate to the new gateway.

Operators must inspect their ignored private values locally and remove obsolete
`credentials.internal.gatewayClientToken`, `credentials.github.pullRequestPat`,
and `management.repositories`, including empty mappings. Remove `githubAuth.mode`
from ordinary overrides and old `GITHUB_AUTH_MODE`/`CODEFACTORY_AUTH_MODE` settings.
Old `CODEFACTORY_AUTH_TOKEN`, `REPO_GITHUB_PAT`, and `PR_GITHUB_TOKEN` runtime
settings and `X-Repository-Token` submissions are rejected, not ignored as
fallbacks. Retire obsolete workflow secrets through separate authorization.
Do not put the PEM or management service token in tracked examples.

**Drain or separately reconcile queued/running PAT-backed tasks before upgrading.**
Old task credential artifacts are unsupported, cannot execute or retry as App
tasks, and are not silently converted. Restoring an old worker template or Secret
does not restore PAT support. Review task results and existing branch/PR artifacts
before separately authorizing migration or new work. This implementation performs
no task recovery, migration, Secret creation, live deployment, or remote edits.

## Independent Credentials

| Purpose | Configuration | Consumers |
|---|---|---|
| Repository access | `githubAuth` policies and external App-key Secret | Gateway mints repository-scoped installation tokens |
| Actions submission | Runner-issued OIDC JWT | Gateway verifies identity and repository binding |
| Management submission/status | External `githubAuth.managementServiceSecret` | Management and gateway only |
| Internal supervisor trust | `credentials.internal.supervisorServiceToken` | Supervisor and gateway only |
| GHCR image pulls | `credentials.github.ghcrReadPackagesPat` or existing pull Secret | Kubelet only, `read:packages` |
| Models | `credentials.models.openaiApiKey` and `coderApiKey` | Respective model clients |
| Human management login | `management.admin` or `management.oidc` | Management only |

GHCR PATs are not repository credentials. The chart still creates the independent
supervisor-service and model-auth Secrets. Model keys may be empty only for
unauthenticated endpoints. Management admin passwords retain the existing minimum
eight-character/special-character rule, without whitespace or angle brackets.
Human OIDC still requires the configured issuer/audience/JWKS and allowed subjects;
invalid OIDC never falls back to Basic. Browser storage does not hold credentials.

## Private Values

Keep shared nonsecret settings in ignored `helm/local.values.yaml`, App/policy,
image and routing settings in `helm/orchestrator/local.values.yaml`, and remaining
credentials in `helm/local.secrets-values.yaml`. Start from the tracked examples
only when the corresponding local file does not exist; never overwrite an existing
private file. Restrict private-file permissions to the operator.

The deployment script layers defaults, shared values, orchestrator-only values,
then private credentials. Both charts receive shared credentials, so both schemas
reject removed credential keys. GHCR Secret creation remains independent:
`registry.create: true` requires a username and read-packages PAT and manages
`ghcr-read-packages-pull-secret` in each namespace. Do not enable creation over
an externally owned Secret or disable it without preparing replacement pulls.

Helm release records contain supplied values and rendered Secrets. Restrict
release/Secret RBAC and backups. Never dump real rendered manifests, run `helm get
values`, enable `--debug` or shell tracing, or send secrets in command arguments.
No Azure Key Vault or managed-identity integration is claimed here.

## Identity And Broker Contract

The gateway verifies RS256 JWTs with PyJWT/cryptography, fixed GitHub issuer and
JWKS endpoints, a five-minute key cache, and no redirects or token-selected keys.
Audience, integer time claims, maximum ten-minute lifetime, immutable repository
and owner IDs, and exact workflow/ref/subject/event tuples must match. Fork/PR
and environment subjects are not accepted by branch-only policies. A trusted
workflow caller is authorized only for its configured repository, including retry.

Management sends identities, never user-supplied repository credentials. Its
service token is an intentionally trusted operator-service credential with broad
task status access; repository submission is still bounded by App policy. This
is not multi-tenant isolation. Separate gateways/namespaces for distinct trust domains.

Immutable task Secrets contain `repository-auth: github-app`, a callback token,
and an expiry timestamp, not an installation token. SQLite holds the assigned
repository, Secret reference/UID, and callback hash, never the PEM or installation
token. Workers use `TASK_REPOSITORY_AUTH` and `X-Task-Token` to request read-only
Contents/Issues/Pull requests tokens. Supervisor requests write-scoped Contents
and Pull requests tokens using only its internal service credential. Both clients
renew tokens in memory before expiration; delayed work does not depend on the
submitting workflow's lifetime.

The broker checks active task state, assigned repository, retained credential
binding, current policy, installation owner/App identity, selected repository
grant, and exact returned permissions. Terminal/cleaned-up tasks cannot renew.
Already-issued installation tokens can remain valid until GitHub expiry; local
cleanup is not remote revocation. Workers remain read-only and cannot request
supervisor grants. No credentials belong in issue text, URLs, model context,
patches, task payloads, policies, or logs. Redaction is not universal secret detection.

## Transport And Isolation

Use verified HTTPS from workflows, workers, supervisor, and management to the
gateway and from credential clients to GitHub. All credential-bearing clients
reject redirects. `gateway.transportMode: edge-tls` means infrastructure enforces
TLS; forwarded headers do not authenticate callers or prove secure transport.
Restrict direct port 8000/Service/NodePort access with enforced network policy or
firewalls. The chart does not install or verify that isolation.

`gateway.ingress.tlsTermination: internal` uses Traefik `websecure` and requires
the configured certificate Secret. `external` uses `web` behind an external TLS
proxy and no in-cluster TLS reference. Set `gateway.ingress.host` to the forwarded
Host, independently of the public HTTPS `supervisor.config.acceptor-url`. Keep
`web` free of global redirects for this route. Verify DNS, certificates, worker
reachability, edge-to-ingress restrictions, and denied backend bypasses separately.
The nonsecret example includes an external route, not complete security isolation.

Runtime `local-http` is loopback-only development and is disallowed by the chart.
Missing/unknown transport modes fail startup. Worker local tests have their own
explicit local-HTTP opt-in; production broker access still requires HTTPS.

## Task Lifecycle

HTTP 202 acknowledges a durable queued task, not completion. Catalog/runtime
pinning and immutable hashed catalogs are unchanged. Duplicate submission
revalidates repository access but cannot replace the Secret, callback, or task.
Worker candidates require the assigned callback and matching task/repository/issue;
only the separately authenticated supervisor can report terminal completion with
a human-review PR. The gateway owns task registration and Secret cleanup, not
worker dispatch or GitHub publication.

Terminal results persist before Secret cleanup. UID-preconditioned deletion never
adopts or deletes a mismatched/unverified Secret. A trusted management service can
request `POST /api/v1/tasks/{task_id}/sync` with `{}` to retry terminal cleanup.
GET exposes `lifecycle` and `credential_cleanup`; failed deletion stays visible as
`cleanup-pending`. Restore the DB and task Secrets consistently. Namespace RBAC
cannot limit dynamic Secret creation/deletion by name prefix; isolate workloads.

For a failed App task, resolve its cause and cleanup before intentionally running
the source workflow with `retry_task_id`. OIDC is bound to the stored repository;
the workflow run ID is its stable retry key. Retry creates one new child task and
callback while preserving history. Running/completed tasks cannot restart; old
PAT tasks require separate operator reconciliation, not this retry mechanism.

`gateway.taskCredentialTtlSeconds` retains its task-secret expiry metadata (default
86400, range 60-604800); it is not GitHub token revocation or Kubernetes GC.
`gateway.taskJobTtlSecondsAfterFinished` controls supervisor-created Job retention
(default 86400, range 1-604800). The suspended seed has no TTL. Finished-Job GC
can remove Pods/logs but not DB records or independently owned task Secrets.
No historical Jobs, image pins, catalog hashes, or credentials are migrated here.

## Verification Boundary

Local signed-JWT, mocked App/broker, orchestrator, and Helm checks do not prove
live App installation grants, TLS/network reachability, namespace isolation,
sandbox support, PVC durability, model behavior, or end-to-end PR publication.
Those are separately authorized deployment acceptance checks. This change does
not create Apps/Secrets, rotate credentials, restart workloads, or submit work.
