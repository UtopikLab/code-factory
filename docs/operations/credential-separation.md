# Repository Authentication And Credential Separation

GitHub App installation tokens are the only supported repository credentials.
GitHub Actions submits work using OIDC; interactive submissions require a GitHub
App user access token verified by the gateway, in addition to management login
and its distinct service token. There is no auth-mode
switch, source PAT intake, shared workflow gateway token, or credential fallback.

This is a local implementation change, not a deployment or credential rotation.
Treat any previously exposed PAT as compromised: its owner must revoke/replace it
and audit consumers through a separately authorized process.

## Operator Setup

Before a separately authorized rollout:

1. Create a GitHub App with Contents and Pull requests read/write, Issues read,
   and implicit Metadata read. Both **Only select repositories** and **All
   repositories** installations are supported; prefer selected repositories for
   least privilege. All repositories broadens App installation access, including
   dynamic eligibility when no repository allowlist is configured, but probe and
   execution tokens remain scoped to one repository. Suspended installations and
   missing or unknown repository-selection modes are rejected.
   Organization approvals, branch rules, and human PR review still apply.
2. Provision its PEM in an externally managed Kubernetes Secret in the
   orchestrator namespace. Helm accepts only the Secret name/key; only the
   gateway mounts it. Management, supervisor, workers, task records, and catalogs
   must never receive the PEM. Restrict Secret/RBAC access and backups, namespace
   workload creation, and enable encryption at rest.
3. Set the App ID and Actions audience. Configure `githubAuth.workflowPolicy`
   with the protected branch, workflow path, and allowed events. Empty or omitted
   `githubAuth.repositories` enables dynamic access through verified App
   installations. An optional nonempty list remains an additional restriction,
   including its immutable repository/owner/installation IDs and exact bindings.
   A restriction denial never falls back to dynamic access. Protect the configured
   branch and workflow files in every installed repository.
4. If management is enabled, provision a separate random service-token Secret,
   at least 32 characters without whitespace, distinct from the supervisor token.
   Only gateway and management reference it. Preserve human Basic/OIDC login;
   neither login method proves GitHub repository access. Enable GitHub App user
   authorization as described below before using interactive submissions.
5. In each source repository install the canonical
   [drain workflow](../../.github/workflows/drain.yaml) through a separately
   authorized remote change. It sets the public gateway URL
   `https://code-factory.utopiklab.com` and audience
   `https://code-factory.utopiklab.com/tasks` directly in its step environment;
   no Actions URL secret or audience variable is required. Verify the audience
   matches `githubAuth.audience`. It always requests OIDC with `id-token: write`.

Use the single [orchestrator example](../../helm/orchestrator/example.values.yaml),
replacing its illustrative App ID and audience. Secret names/keys are chart defaults;
override them in the ignored `helm/orchestrator/local.values.yaml` only when needed.
Provision every referenced Secret out of band. App-key and supervisor-token refs
are always required; management refs are required when management or its OAuth
integration is enabled. Image-pull Secrets are required in both namespaces.
The deploy script checks each rendered Secret name, type, and required data key
before either chart upgrade begins. It never creates Secrets or receives their
contents.

## Dynamic Authorization

Interactive submission and retry require a GitHub App user token. The gateway
calls `/user`, verifies a real GitHub user, verifies that the user's App
installations include this App's repository installation, and requires live
repository `permissions.push`. Client-supplied login, IDs, or permissions are
not accepted. The App must independently have access to the repository.
GitHub API failures, revocation, forks, archived repositories, and suspended or
all-repository installations fail closed. Installation discovery uses the
App-authenticated repository installation endpoint; repository and owner IDs
come from GitHub, not request bodies.

Actions identity is **not human authorization**. The gateway verifies GitHub's
issuer, audience, signature and token times, exact workflow/ref/subject/event,
repository and owner IDs, and App access. Shared policy permits only `issues`
and `workflow_dispatch`; pull requests, `pull_request_target`, arbitrary workflow
paths, and forks are denied. Treat the configured workflow and protected branch
as trusted automation: the canonical issue workflow can process issues opened
by outside contributors. OIDC does not prove the issue author's write access.

```yaml
githubAuth:
   # Keep your verified appId and your dedicated Actions audience.
   workflowPolicy:
      ref: refs/heads/main
      workflow: .github/workflows/drain.yaml
      events: [issues, workflow_dispatch]
   repositories: []
```

Execution still uses fresh, short-lived installation tokens with exactly one
repository ID and explicit read or write permissions. Installation and repository
access are revalidated during credential renewal. User tokens are never stored
in task records, sent to workers, or used as execution credentials. User access
is rechecked at submission/retry, not continuously after acceptance.

## Interactive GitHub Login

In the **same GitHub App**, enable user authorization and expiring user access
tokens. Register the exact callback
`<management.publicOrigin>/auth` and provision its
OAuth client secret out of band in an external Kubernetes Secret. Set:

```yaml
management:
   github:
      clientId: "" # Set the App's OAuth client ID, not its numeric App ID.

secretRefs:
   managementGithubClientSecret:
      name: <pre-provisioned-secret-name>
      key: client-secret
```

The client secret is referenced only by management, never by gateway or workers.
Do not place its contents in tracked values. Management can still show status
without OAuth setup, but interactive submissions fail closed until configured
and connected. Use **Connect GitHub** in the task dialog; a dynamic configuration
accepts an owner/repository name and authorizes it at the gateway.

The server exchanges the one-use authorization code with state, a browser-bound
cookie, PKCE, and the authenticated management subject. The browser receives
only an opaque Secure/HttpOnly/SameSite session cookie, never an access or refresh
token. Sessions last at most one hour (or the grant's shorter lifetime); refresh
tokens are not retained. Disconnect removes the local session, not the GitHub
App authorization. Revoke that separately in GitHub if necessary.

Sessions and pending login state are process-local and bounded. Keep one
management process/replica; restarts require reconnection. Shared durable session
storage is not implemented. HTTPS is required. A generic OIDC proxy must preserve
the same verified subject across login start and callback. Management OIDC's
audience is independent of `githubAuth.audience`, which remains required for
Actions. Configure the real audience in local values; empty repositories no
longer require fabricated repository IDs, but an empty audience is still invalid.
Ensure ingress/proxy logs redact callback queries; the management container
already disables access logs. No live GitHub App, Secret, or deployment setup
is performed by this change.

## Breaking Rollout

Rebuild and explicitly select compatible **gateway, supervisor, worker, and
management** images together before an authorized rollout. Existing image pins
are intentionally untouched. Install the updated source workflow and policy
bindings together; an old client will not authenticate to the new gateway.

The chart API is reference-only. Raw PEMs, PATs, passwords, API keys, and
`registry.create` values are rejected by strict schemas. Secret names and keys
come from chart defaults; change references only in
`helm/orchestrator/local.values.yaml` when needed. Provision fresh Secrets before
rollout; do not point at Secrets owned by
the old release, since the clean-break chart removes Helm-managed Secret objects.
The preflight checks rendered Secret names, types, and required data keys before
either chart is upgraded.

**Drain or separately reconcile queued/running PAT-backed tasks before upgrading.**
Old task credential artifacts are unsupported, cannot execute or retry as App
tasks, and are not silently converted. Restoring an old worker template or Secret
does not restore PAT support. Review task results and existing branch/PR artifacts
before separately authorizing migration or new work. Secret contents remain an
out-of-band operator responsibility.

## Independent Credentials

| Purpose | Configuration | Consumers |
|---|---|---|
| Repository access | `githubAuth` shared policy and `secretRefs.githubAppKey` | Gateway mounts the externally provisioned App PEM and mints repository-scoped installation tokens |
| Interactive GitHub identity | `management.github` OAuth client and external client-secret reference | Management keeps user tokens server-side; gateway verifies live access |
| Actions submission | Runner-issued OIDC JWT | Gateway verifies identity and repository binding |
| Management submission/status | `secretRefs.managementServiceToken` | Management and gateway only |
| Internal supervisor trust | `secretRefs.supervisorServiceToken` | Supervisor and gateway only |
| GHCR image pulls | `imagePullSecrets` in each namespace | Kubelet only; pull Secret is externally provisioned |
| Models | Optional `secretRefs.openaiApiKey` and `secretRefs.coderApiKey` | Respective model clients |
| Human management login | `secretRefs.managementAdminPassword` or `management.oidc` | Management only |

The charts never create credential Secrets. Model refs may be empty only when the
configured endpoint is unauthenticated. Management admin password policy is
validated by the management service at startup; the chart validates its reference.
Human OIDC still requires the configured issuer/audience/JWKS and allowed subjects;
invalid OIDC never falls back to Basic. Browser storage does not hold credentials.

## Values Layout

Orchestrator configuration has chart defaults, one tracked starting example at
`helm/orchestrator/example.values.yaml`, and one operator-local override at
`helm/orchestrator/local.values.yaml`. The example omits `secretRefs`, so the
reference-only chart defaults remain authoritative unless the local override
intentionally changes a Secret name or key. The deploy script applies chart
defaults plus the local override; copy desired nonsecret example settings into
that local file before deployment. Inference normally uses its chart defaults.

Secret references are names and data-key identifiers, never secret material.
Keep credential values out of all Helm values. The local override is ignored by
Git; restrict its permissions to the operator (`chmod 600`). Avoid a second
override or refs file, since later Helm values silently replace earlier values.
Never copy illustrative App IDs or repository policy into a real deployment.
Missing audience and App identity must be filled with verified inputs; optional
repository restrictions need verified IDs and bindings.

Local checks: `python3 -m unittest discover -s helm -p 'test_*.py' -v` tests the
charts and default/example/local layering. Run `helm lint helm/inference` and
`helm lint helm/orchestrator --values helm/orchestrator/example.values.yaml
--values helm/orchestrator/local.values.yaml` for chart validation. The deploy
preflight checks live Secret names, data keys, and types without printing Secret
data.

Helm release records still contain nonsecret names and settings, but no credential
values or rendered credential Secrets. Restrict release/Secret RBAC and backups.
Never dump rendered manifests, run `helm get values`, enable `--debug` or shell
tracing, or send secrets in command arguments. No secret-manager integration is
installed by this repository.

## Secret Provisioning

The charts only reference pre-provisioned Kubernetes Secrets. They never create
credential Secrets or accept credential values. For the current DEV process,
the repository provides one local plaintext dotenv file and one apply command;
it does not install an External Secrets operator or encrypt the local file.

| Secret | Namespace | Required data key | Purpose |
|---|---|---|---|
| `code-factory-ghcr-pull` | `inference`, `orchestrator` | `.dockerconfigjson` | GHCR image pulls; type `kubernetes.io/dockerconfigjson` |
| `code-factory-github-app-key` | `orchestrator` | `private-key.pem` | GitHub App PEM; mounted only by gateway |
| `code-factory-supervisor-auth` | `orchestrator` | `token` | Independent gateway/supervisor service token |
| `code-factory-management-service` | `orchestrator` | `token` | Management/gateway service token when management is enabled |
| `code-factory-management-admin-password` | `orchestrator` | `password` | Human admin login when admin auth is enabled |
| configured model-key Secret | `orchestrator` | `api-key` | Optional OpenAI/coder endpoint credentials |
| configured OAuth client Secret | `orchestrator` | `client-secret` | Optional management GitHub OAuth |

Secret names and key identifiers live in chart defaults. The single tracked
orchestrator example inherits those references, while local nonsecret overrides
belong in `helm/orchestrator/local.values.yaml`. The tracked
[dotenv template](../../helm/example.secret-bundle.env) contains placeholders
only. Make the ignored local copy, replace every required placeholder with
current DEV credentials, and keep the file mode restricted:

```bash
umask 077
cp helm/example.secret-bundle.env helm/local.secret-bundle.env
chmod 600 helm/local.secret-bundle.env
${EDITOR:-vi} helm/local.secret-bundle.env
./helm/apply-secret-bundle.sh
```

The local file is plaintext on disk, is excluded by `.gitignore`, and is never
committed. The apply command refuses a tracked or non-ignored file, requires a
regular file owned by the current user, and changes its permissions to `0600`
before reading it. Do not put credentials in command arguments, shell history,
tracked files, or logs; protect the workstation and its backups accordingly.
Keep dotenv values double-quoted and encode PEM line breaks as `\\n`, as shown.
Use a read-packages-only GHCR token, independent random management and supervisor
tokens, the intended GitHub App PEM, and a valid management admin password.
Leave optional model/OAuth values empty unless needed. Update the local copy
directly when credentials change; do not create a second format or fallback path.

Applying the bundle provisions external Secrets and namespaces only. Deployment
remains separate:

```bash
./helm/apply-secret-bundle.sh
./helm/deploy.sh
```

The apply command strictly parses the local dotenv file without sourcing or shell
expansion, renders Kubernetes objects in memory, and pipes them directly to
`kubectl apply`; it does not print credential values. It uses chart-configured
namespaces, pull Secret names, and orchestrator Secret names/data keys, and
applies optional model/OAuth credentials only when nonempty. Model and OAuth refs
may be empty only when those integrations do not need a key. The deploy command
checks Secret names, types, and required keys before upgrading either release.

`helm/deploy.sh` renders each chart and verifies Secret existence, namespace,
type, and required data keys before either release upgrade begins. It does not
read or print Secret values. It then upgrades both charts concurrently and keeps
only one Helm history revision. That history limit is destructive to rollback
capability, but removes prior credential-bearing release revisions after an
upgrade. Rotate any credential previously supplied through Helm values, especially
the GHCR PAT, at its issuing system; deleting local files or old Helm revisions is
not credential rotation. Apply the same retention and rotation policy to backups.

Use separate random values for the management and supervisor tokens. Restrict
Secret read/write RBAC, encrypt etcd and backups, use read-only mounts for file
credentials, and avoid placing values in shell arguments or logs. Kubernetes
Secret data is base64-encoded, not encrypted by that encoding.

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
binding, current policy, installation owner/App identity, selected/all installation
mode, single-repository token grant, and exact returned permissions. Terminal/cleaned-up tasks cannot renew.
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
