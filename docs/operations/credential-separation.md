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
   and implicit Metadata read. Install it with **Only select repositories** for
   each trusted owner. All-repository or suspended installations are rejected.
   Organization approvals, branch rules, and human PR review still apply.
2. By default, provision its PEM in an externally managed Kubernetes Secret in
   the orchestrator namespace and configure only the Secret name/key in values.
   Alternatively, explicitly opt into the inline PEM mode described below. Only
   the gateway mounts the private key; management, supervisor, workers, task
   records, and catalogs must never receive it. Restrict Secret/RBAC access and
   backups, namespace workload creation, and enable encryption at rest.
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
5. In each source repository set the Actions secret `CODEFACTORY_URL` to the
   trusted HTTPS gateway base URL and variable `CODEFACTORY_OIDC_AUDIENCE` to
   exactly `githubAuth.audience`. Install the canonical
   [drain workflow](../../.github/workflows/drain.yaml) through a separately
   authorized remote change. It always requests OIDC with `id-token: write`.

Use the nonsecret [orchestrator example](../../helm/orchestrator/example.values.yaml),
replacing its illustrative App ID and audience, together with external
Secret references from the [private example](../../helm/example.secrets-values.yaml).
App setup is mandatory even when management is disabled. Its service-token
reference is required only when management is enabled. Missing or unknown auth
settings fail closed; blank defaults intentionally cannot deploy.

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
`<management.publicOrigin>/management/api/github/callback` and provision its
OAuth client secret out of band in an external Kubernetes Secret. Set:

```yaml
management:
   github:
      clientId: "" # Set the App's actual OAuth client ID, not its numeric App ID.
      clientSecret:
         name: "" # Set the externally provisioned Secret name.
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
| Repository access | `githubAuth` shared policy, optional restrictions and external or inline-managed App-key Secret | Gateway mints repository-scoped installation tokens |
| Interactive GitHub identity | `management.github` OAuth client and external client-secret reference | Management keeps user tokens server-side; gateway verifies live access |
| Actions submission | Runner-issued OIDC JWT | Gateway verifies identity and repository binding |
| Management submission/status | External `githubAuth.managementServiceSecret` | Management and gateway only |
| Internal supervisor trust | `credentials.internal.supervisorServiceToken` or `supervisorServiceSecret` | Supervisor and gateway only |
| GHCR image pulls | `credentials.github.ghcrReadPackagesPat` or existing pull Secret | Kubelet only, `read:packages` |
| Models | `credentials.models.openaiApiKey` / `openaiSecret` and `coderApiKey` / `coderSecret` | Respective model clients |
| Human management login | `management.admin.password` / `passwordSecret` or `management.oidc` | Management only |

GHCR PATs are not repository credentials. By default the chart creates independent
supervisor-service and model-auth Secrets; external references opt out per Secret.
Model keys may be empty only for
unauthenticated endpoints. Management admin passwords retain the existing minimum
eight-character/special-character rule, without whitespace or angle brackets.
Human OIDC still requires the configured issuer/audience/JWKS and allowed subjects;
invalid OIDC never falls back to Basic. Browser storage does not hold credentials.

## Private Values

Each setting has one override owner (chart defaults remain in each chart):

| Local file | Owns | Tracked starting point |
|---|---|---|
| `helm/local.values.yaml` | Shared nonsecret settings, such as registry creation and username | `helm/example.values.yaml` |
| `helm/orchestrator/local.values.yaml` | App ID, OIDC audience and repository policy; images, routes, runtime behavior; management auth mode and human OIDC settings | `helm/orchestrator/example.values.yaml` |
| `helm/local.secrets-values.yaml` | Independent credentials, management admin login, App-key source and `githubAuth.managementServiceSecret` reference | `helm/example.secrets-values.yaml` |

Secret references are not secret material, but are intentionally kept beside
credential configuration in the private override. By default the App PEM remains
in a separately provisioned external Secret; the management service token always
does. Inline App PEM is an explicit alternative, not a change to that default.
Start from the tracked examples only when the corresponding local file does not
exist; never overwrite an existing private file. All three local paths are ignored
by Git. Restrict local-file permissions to the operator (`chmod 600`).

The deployment script layers defaults, shared values, orchestrator-only values,
then private credentials. Both charts receive shared credentials, so both schemas
reject removed credential keys. GHCR Secret creation remains independent:
`registry.create: true` requires a username and read-packages PAT and manages
`ghcr-read-packages-pull-secret` in each namespace. Do not enable creation over
an externally owned Secret or disable it without preparing replacement pulls.

Keep settings out of multiple override files: the last value wins and can hide a
stale value earlier in the chain. Never copy illustrative App IDs or repository
policy into a real deployment. Missing audience and App identity must be filled
with verified inputs; optional repository restrictions need verified IDs and
bindings. Reorganizing files does not make them valid.

Local checks: `python3 -m unittest discover -s helm -p 'test_*.py' -v` tests the
charts and example layering using synthetic credentials. For actual configuration,
lint inference with shared values and secrets; lint orchestrator with shared,
orchestrator-only, then secrets values, in that order. Capture and redact failures
locally: Helm schema errors can echo private values. Tests do not verify that
referenced external Secrets exist or contain the required keys.

Helm release records contain supplied values and rendered Secrets. Restrict
release/Secret RBAC and backups. Never dump real rendered manifests, run `helm get
values`, enable `--debug` or shell tracing, or send secrets in command arguments.
No Azure Key Vault or managed-identity integration is claimed here.

## Inline App PEM

Merge these fields into the existing `githubAuth` mapping in
`helm/local.secrets-values.yaml`; preserve its management-service reference and
all other credentials. Replace the placeholder line with the entire PEM, with
each line indented under `|`. The placeholder is not a usable private key.

```yaml
githubAuth:
   privateKeyPem: |
      REPLACE_WITH_YOUR_COMPLETE_PEM
   privateKeySecret:
      name: code-factory-github-app
      key: private-key.pem
```

The `privateKeySecret` mapping is optional for nonblank inline PEM: omitting it
uses `code-factory-github-app` with key `private-key.pem`. An empty name also uses
that inline default. A supplied name/key overrides the destination; no rename is
required. Only orchestrator renders this Opaque Secret in its configured namespace,
and only the gateway mounts it. Base64 encoding preserves the literal block's
newlines exactly but is not encryption. Inference accepts the shared private
values without rendering an App-key Secret. No `lookup` reads existing Secrets.
The gateway validates the actual key at runtime; Helm checks the value type and
Secret reference, not cryptographic validity.

Nonblank `privateKeyPem` automatically creates the Secret. Missing, empty, or
whitespace-only PEM does not create it and uses the existing `privateKeySecret`
name/key instead; external mode requires a nonempty, valid reference name and key.
Whitespace is checked only for source selection; actual PEM
bytes and newlines are preserved. External mode remains the default: clear PEM
to `""` in every layer and retain the existing Secret reference.
Do not adopt an externally owned Secret in place without a
separately authorized ownership/migration plan. Switching away from inline mode
removes the Helm-managed Secret on upgrade; prepare an externally managed
replacement and reconcile references before that rollout.

**Inline PEM enters Helm values and release history**, including the inference
release when the shared private layer is supplied to both charts. Keep the local
file ignored and operator-readable only, restrict release/Secret access with
least-privilege namespace-scoped RBAC, enable etcd encryption at rest, and protect
backups. Never print real rendered manifests or debug output. Existing-Secret
mode avoids passing the PEM to Helm only when all raw PEM values are cleared;
it does not erase older release records or backups.

## External Credential Migration

The three values layers and deployment script do not need to change. Optional
`name`/`key` references in the private layer let Kubernetes supply credentials
without passing their contents to Helm. An empty reference name preserves the
current Helm-managed Secret. A nonempty name skips that Secret's creation and
uses `secretKeyRef` in every relevant workload. Raw values and external references
for the same credential are mutually exclusive; clear the raw value in every
layer. No Helm `lookup` or secret-manager controller is used or installed.

Conventional names are included as a commented migration block in the
[private example](../../helm/example.secrets-values.yaml). They are not active
defaults or verified live resources:

| Reference | Proposed Secret | Key | Namespace |
|---|---|---|---|
| `credentials.internal.supervisorServiceSecret` | `code-factory-supervisor-service` | `token` | `orchestrator` |
| `credentials.models.openaiSecret` | `code-factory-openai` | `api-key` | `orchestrator` |
| `credentials.models.coderSecret` | `code-factory-coder` | `api-key` | `orchestrator` |
| `management.admin.passwordSecret` | `code-factory-management-admin` | `password` | `orchestrator` |

Before a separately authorized migration:

1. Provision the needed Secrets through an approved out-of-band process. Use
   separate names from Helm-owned Secrets: switching modes removes the old
   managed resources on upgrade. Do not attempt to adopt them in place. Keep
   the supervisor token independent from the management service token. Admin
   passwords must still meet the same runtime password rules. Provision model
   keys only when needed, or leave the existing empty-key behavior for public
   endpoints. Restrict Secret access with namespace-scoped least-privilege RBAC,
   enable etcd encryption at rest, and protect backups; base64 is not encryption.
2. Verify Secret existence, type, required keys, and credential validity privately
   without dumping contents. These local render tests do not perform that check.
   Keep the existing `code-factory-github-app` / `private-key.pem` and
   `code-factory-management-service` / `token` references; verify them too.
3. Merge the desired reference mappings into the existing private values file,
   setting their raw fields to `""`. Do not copy a duplicate `credentials` or
   `management` mapping into YAML or overwrite existing local files. For admin
   mode, keep the username and set only its password source; OIDC is unchanged.
4. For GHCR, existing pull-Secret support already suffices. Provision a separate
   externally owned `kubernetes.io/dockerconfigjson` Secret in **both** namespaces
   first, for example `code-factory-ghcr-pull`. Set shared `registry.create: false`
   and `registry.username: ""`, clear `credentials.github.ghcrReadPackagesPat`,
   set shared `inference.imagePullSecrets: [{name: code-factory-ghcr-pull}]`, and
   set orchestrator-only `imagePullSecrets: [{name: code-factory-ghcr-pull}]`.
   These names also require verification before use.
5. During an authorized rollout, supply all three layers without reusing old
   secret-bearing values (`--reuse-values`), and verify startup, authentication,
   and image pulls. Environment-sourced credentials require workload recreation
   to pick up later Secret updates; running task Jobs need separate reconciliation.

External references prevent credential contents entering **new** release records
only after all raw credential fields are cleared from supplied values. Old Helm
revisions and backups still retain earlier values and rendered Secrets. Review
retention and rollback implications and separately authorize credential rotation
and historical-record handling; this change does not delete history, revoke
credentials, provision Secrets, change local settings, or deploy anything.

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
