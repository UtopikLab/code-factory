# Multi-Repository Migration Plan

**Status:** Proposed, 2026-10-23. Documentation only, not approval to execute.

## Scope And Evidence

Target ownership agreed for this plan:

| Alias | Repository | Responsibility |
| --- | --- | --- |
| App | <https://github.com/UtopikPrompt/code-factory> | Application source, orchestrator chart, application artifacts |
| Inference | <https://github.com/UtopikLab/inference-platform> | Inference chart and upstream runtime integration |
| Infra | <https://github.com/UtopikLab/infra-codefactory> | Production configuration, version selection, provisioning and deployment operations |

Local inspection establishes two charts, four application Docker build contexts,
and GitHub Actions CI. Both chart versions are currently `0.1.0`. Application
defaults use `latest`; inference uses an upstream llama.cpp image digest. The
inference dependency is NVIDIA device-plugin `0.20.1`. CI builds application
images on PRs and publishes on main pushes to `ghcr.io/utopiklab/code-factory-*`
with a seven-character commit tag and `latest`; it does not publish charts.
Chart `sources` still name `UtopikLab/code-factory`, not the agreed App URL.

[Deployment notes](deployment-summary.md) describe single-node K3s and prepared,
not necessarily deployed, changes. They are incident evidence, not a fresh live
baseline. The root [README](../../README.md), build guides and
[GPU ownership runbook](gpu-provider-managed-migration-runbook.md) include legacy
paths such as `inference/deploy.sh` and `helm` as one chart; do not execute them
as migration instructions. No remote contents, registry access, cluster state,
tracking state or dirty-worktree status were verified for this plan.

## Ownership And Exact Path Mapping

Paths below are relative to each named repository. These are proposed target
paths, not claims about existing remote trees. Copy approved, sanitized source
first; remove legacy copies only in the final cleanup phase.

| Current path | Target owner and path | Rule |
| --- | --- | --- |
| `orchestrator/{gateway,management,supervisor,worker}/**` | App: unchanged | Keep Dockerfiles, requirements, tests, UI and supervisor README together; exclude caches, builds and local files. |
| `helm/orchestrator/**` | App: unchanged | Templates, schema, catalog/schema, dashboard and neutral examples stay with application code; exclude `local.values.yaml`. |
| `helm/inference/**` | Inference: unchanged | Templates, schema, `Chart.yaml`, `Chart.lock` and reviewed chart files; do not import downloaded `charts/*.tgz` as source history. Rebuild locked dependencies. |
| `helm/deploy.sh`, `helm/workload-readiness.jq`, `helm/test_deploy_readiness.py` | Infra: same paths | Operational orchestration and readiness checks; replace sibling-source-chart assumptions with explicitly selected unpacked artifacts before cutover. |
| `helm/apply-secret-bundle.sh`, `helm/render-secret-bundle.py`, `helm/example.secret-bundle.env`, `helm/test_secret_bundle.py` | Infra: same paths | One canonical provisioning implementation; template placeholders only. Adapt hard-coded chart paths and namespace checks against selected artifacts, not copied defaults. |
| `helm/validate_secret_refs.py`, `helm/test_validate_secret_refs.py` | Infra: same paths | Canonical live preflight validator; source charts own reference/schema contracts. Test against both pinned chart packages; do not fork validators in each source repo. |
| `helm/orchestrator/local.values.yaml`, `helm/local.secret-bundle.env` | No Git destination | Never copy ignored files or their history. Reconstruct approved nonsecret settings in Infra; provision credentials out of band. |
| `.github/workflows/ci.yml` | App: same path; Inference/Infra: owner-specific CI in `.github/workflows/ci.yml` | Retain GitHub Actions. App keeps Python/image jobs and gains chart release validation/publication; Inference validates/publishes its chart; Infra validates selected combinations, never rebuilds source images. |
| `.github/workflows/drain.yaml` | App: canonical workflow at same path | Task intake, not deployment automation. Add to other repos only if they intentionally submit tasks, with separately approved App/OIDC bindings. |
| `.git-hooks/pre-commit`, `.gitignore` | App: retained; Inference/Infra: reviewed owner-specific equivalents | Carry relevant checks/exclusions, not blind copies. Never copy `.git/`, virtualenvs, caches or browser results. |
| `docs/build/{build-plan,code-alignment-plan}.md`, `docs/design/**` | App: same paths | Application design/history remains; split any verified inference-only instructions into Inference `docs/build/inference-build.md`, replacing stale paths with links. |
| `docs/infra/k8s-node01.md` | Infra: same path | Host, K3s, storage and GPU operating facts; sanitize environment details according to repository visibility. |
| `docs/hardware-telemetry.yaml` | Infra: same path, reference-only initially | Helm-style template under docs, not evidence of an installed exporter. Do not activate or package it during extraction; validate/redesign only under separate authorization. |
| `helm/orchestrator/templates/metrics-services.yaml`, `helm/orchestrator/dashboards/grafana-dashboard.json` | App: unchanged | Application metrics contract and generic dashboard stay with source. Actual scrape targets, NodePorts, network exposure and dashboard installation belong to Infra configuration. |
| `docs/operations/{deployment-summary,gpu-provider-managed-migration-runbook}.md` | Infra: same paths | Preserve dated incident evidence; correct obsolete chart paths in a separate reviewed documentation PR. GPU provider ownership is not changed by this split. |
| `docs/operations/credential-separation.md` | App: same path; Infra: `docs/operations/credential-provisioning.md` | App retains auth/API/compatibility contract; Infra receives operational provisioning sections and cross-links, not a competing auth specification. |
| `docs/operations/{multi-repo-setup,new-repo-setup}.md` | App: unchanged | Task repository onboarding and trust policy, not infrastructure repository bootstrap. Link Infra operating procedures where needed. |
| This migration plan | App: unchanged | Infra links the approved version; approval/cutover evidence recorded in Infra `docs/operations/migration-cutover.md`. |
| Root `README.md` | Each owner: own `README.md` | App becomes application entry point; Inference documents its packaged chart/API; Infra documents configuration and operations. Replace stale guidance in follow-up PRs, not in this change. |

Source chart `values.yaml` and tracked examples must become environment-neutral:
no production hostnames, node paths, actual App/repository IDs, storage choices,
or production scrape destinations. Keep schemas, reference-only Secret defaults,
and illustrative examples with source. Preserve today's effective settings in
Infra `environments/production/{orchestrator,inference}.values.yaml` before
neutralizing defaults. Every value removed from defaults needs a reviewed value
mapping; separating files must not silently change the effective runtime.

Infra's proposed selection record is
`environments/production/release-set.yaml`: chart locations/versions/digests,
four application image digests, upstream inference digest, source commit IDs,
configuration commit, dependency lock identity, catalog/runtime hashes and
credential-generation references (identifiers only). This is a release record,
not a new controller or CI platform. Operator-local overrides remain ignored,
outside this record; approved nonsecret production settings must be reproducible.

## Artifact And Compatibility Contract

1. Each source repo publishes its own chart using independent SemVer releases.
   Breaking values/schema or runtime contract changes require a major version
   and migration notes; compatible additions use minor, fixes use patch.
   Retain chart names `orchestrator` and `inference`. Set chart `appVersion` to
   the associated application release/upstream runtime identifier; it is not a
   substitute for image digests. Correct `sources` metadata in source PRs.
2. App publishes the four images from one tested source revision. Preserve
   existing image names only if publishing access is proven. Record full commit
   provenance and resolved digests; production selects `repository@sha256:...`,
   never `latest` or a mutable short-SHA tag. Inference publishes deployment
   artifacts, not an unsolicited llama.cpp fork/image pipeline; retain the
   approved upstream runtime digest and locked device-plugin dependency.
3. Agree OCI chart locations and package visibility before publication. Do not
   assume `UtopikPrompt` Actions `GITHUB_TOKEN` can write UtopikLab packages.
   Owners must either grant supported cross-org package access, approve a
   narrowly scoped publishing credential, or select an App-owned registry path.
   Verify push, Infra chart download, and kubelet image pull independently;
   source read access and GitHub App execution tokens do not imply registry access.
4. Infra selects already-published chart versions **and** archive/OCI digests;
   it never deploys branches, floating chart versions or sibling working trees.
   Retain prior packages/images and locked dependencies for the rollback window.
   Publishing must complete and availability checks pass before a selection PR
   can merge. Record architecture/platform support for the K3s node.
5. Each release states compatible chart, gateway/supervisor/worker/management,
   task DB schema, task snapshot/catalog version and inference API combinations.
   Initially test/select all four App images as a set; do not independently bump
   worker or gateway across the existing credential-breaking rollout boundary.
   Do not invent a schema version: record the actual baseline and tested readers.
6. Preserve service names, release names/namespaces, resource identities, PVC
   attachment, endpoints/TLS origins and Secret name/key/type contracts initially.
   Catalog names derive from content/schema/runtime hashes; retain historical
   immutable ConfigMaps and RBAC needed to read them. A chart version bump alone
   does not make stored tasks compatible or update their pinned runtime.
7. Inference compatibility includes OpenAI chat and native `GET /models` /
   `POST /models` preparation against the **same per-GPU router**. Preserve
   coder/judge endpoints and flags. Current native HF `owner/repo:quant` downloads
   follow upstream `main`; chart/image pinning does not pin model weights.
   Record tested cache/model checksums where available and disclose this remaining
   reproducibility limit; do not introduce a model downloader redesign here.

## Sequenced PRs And Approval Gates

| Phase | Proposed PRs and actions | Gate before proceeding |
| --- | --- | --- |
| 0: Baseline | App documentation PR approves this plan; authorized operator inventories dirty changes by filename, exact source commits, effective nonsecret values, resource/storage identities, tasks and artifact digests. Separate owners decide repository visibility/access, history and registry location. | No extraction from unreviewed dirty files; no live change. Existing runtime path remains authoritative. |
| 1: Contracts | App/Inference source PRs add release metadata, chart checks and neutral examples; Infra bootstrap PR defines release-set/value layout and contract tests. Hold default changes that would affect legacy deployment until explicit legacy values are provided and equivalence-tested. | Chart/API/Secret contracts and version matrix reviewed; no new deployment authority. |
| 2: Publish | Inference extraction PR imports approved chart source and locks; App PR enables orchestrator chart publication alongside four tested images. Publish candidate versions after authorized merges. | Both orgs' artifact access proven, digests captured, predecessor artifacts retained; current deploy path unchanged. |
| 3: Assemble | Infra PR imports shared operations/test suite, adapts chart-path inputs, selects candidate artifacts and reconstructs reviewed nonsecret production values. Keep legacy tools/charts available; no automatic deploy trigger. | Render equivalence or explicitly approved differences; fixture-based tests and isolated integration pass. |
| 4: Rehearse | Infra evidence PR records staging/end-to-end checks, backup/restore rehearsal, task/catalog compatibility, deployment ordering and rollback set. Use isolated names/storage/trust; never attach a second gateway to the production SQLite store. | Named operator, maintenance window, verified restore and explicit production cutover approval. |
| 5: Cut Over | Separately authorized operator closes intake, drains/reconciles tasks, takes consistent backup, then applies the pinned Infra set. Keep current Helm release names and namespaces; change deployment authority, not resource ownership. | One writer/deployer only; acceptance checks below pass before intake reopens. |
| 6: Retire | After agreed observation/rollback window, App cleanup PR removes extracted inference/shared ops sources and replaces links; update all three READMEs/build guides and CI path filters. | Operators confirm no consumer uses legacy paths. Preserve rollback artifacts, catalogs and protected backups per retention policy. |

History proposal: use a reviewed, path-filtered export in a disposable clone,
limited to approved commits, only if a full-history secret/visibility audit passes.
Otherwise use a sanitized snapshot import with original commit provenance.
Never rewrite this working repository or force-push as part of extraction.
Ignored today does not mean never tracked historically. A discovered credential
requires incident handling/rotation and approval, not silent history copying or
destructive cleanup. The current dirty working tree must remain untouched.

## Secrets, State And Cutover Procedure

- Ignore rules name `local.values.yaml` and `local.secret-bundle.env`; actual
  index/history status remains unverified. A separately commissioned worker must
  run `git ls-files -- helm/local.secret-bundle.env helm/orchestrator/local.values.yaml`
  (expected: no paths), and
  `git check-ignore -v -- helm/local.secret-bundle.env helm/orchestrator/local.values.yaml`
  (expected: matching rules). Review history safely without printing contents;
  stop extraction on any tracked/historical secret finding.
- Never copy local overrides, dotenv files, rendered Secrets, Helm release
  records, task databases or credential-bearing backups into repository history.
  Audit approved exports for secrets. Re-enter nonsecret production settings by
  reviewed key; transfer credentials only through approved out-of-band channels.
  Preserve external namespace-scoped Secret provisioning, restricted file modes,
  RBAC and encryption/backup policy; do not add a secret-management platform.
- Before cutover, verify live single-replica SQLite persistence, actual PVC/path,
  schema and journal mode, mount ownership and reclaim policy. Checked-in code
  sets journal mode `DELETE`; the gateway template uses one replica and defaults
  to PVC `orchestrator-task-state`. The runtime default DB path is
  `/data/tasks/tasks.sqlite3`. Stop new issue/manual/management intake
  through an operator-approved maintenance procedure; wait for accepted tasks to
  finish or explicitly reconcile them. Account for claims, leases, worker Jobs,
  retries and already-created branches/PRs. Do not kill/replay work blindly.
- For outstanding tasks, preserve DB rows, job templates/snapshots, historical
  catalog ConfigMaps plus content/schema/runtime bytes and hashes, callback
  origins, credential references/UID bindings and broker-policy compatibility.
  Recreating a same-named Secret does not preserve its UID; test recovery of
  retained task credentials explicitly rather than rebinding records. Old PAT tasks
  cannot silently become App-backed tasks. The dated deployment notes identify
  old callback DNS/catalog-read failures; resolve or explicitly quarantine these
  before claiming drain success. Never rewrite pins or catalog contents in place.
- Take an encrypted, access-controlled consistent SQLite backup using SQLite's
  backup API or a fully quiesced store including relevant WAL state; never copy
  only a live main DB file. Include catalog/state metadata and independently
  protected credential recovery references. Test integrity and restore on an
  isolated volume with no production callbacks/Jobs. Record RPO/RTO and ownership.
- Provision/verify Secrets before upgrades without displaying data. Preserve
  GitHub App installation IDs, workflow/ref/event bindings and audience, TLS,
  OAuth callback/public origins and separate human/service/model/pull credentials.
  Rotation is separate authorization; management restarts may require GitHub
  reconnection because its sessions are process-local.
- Deploy only changed dependencies in approved order: external prerequisites and
  Secrets, compatible inference (if changed), verify routers, then the coherent
  App set and retained catalogs. If new inference is incompatible with the old
  App, keep intake closed and all consumers quiesced until both are ready.
  No simultaneous legacy/Infra deployers or overlapping old/new runtime writers.
  Retain existing release/resource names to avoid Helm adoption/PVC collisions.
- Do not invoke current `helm/deploy.sh` as the cutover command: it upgrades both
  charts concurrently, deletes worker Jobs/completed or failed pods and uses
  `--history-max 1`. Infra must first provide reviewed sequential artifact-based
  commands, safe Job handling, protected diagnostics and rollback retention.
  Retention must not revive historical credential-bearing release revisions.

## Validation And Acceptance

Commands below are dispatcher checks, **not executed for this documentation PR**.
Use clean workspaces, declared Python dependencies, Helm 3, jq and frontend tools.
Before extraction run checks at current paths; afterwards run each in its owner
repo, with Infra's chart inputs resolved from the selected release set.

| Check | Command or acceptance evidence |
| --- | --- |
| Existing App CI gates | `ruff check orchestrator/`; `pytest orchestrator/ -v -ra --durations=10 --junitxml=test-results/pytest.xml` |
| Repository hook gates | `python3 -m pytest`; `ruff check .` (use configured virtualenv). Do not run broad discovery on the operator workspace: current secret-bundle tests load the protected local values file. First make clean-fixture coverage independent of local files in the authorized tooling PR. |
| Shared tooling | In a clean fixture workspace after that test adaptation: `python3 -m unittest discover -s helm -p 'test_*.py' -v`; require no unexpected skips, including jq readiness checks. Cover defaults/overlays, missing Secret refs/type/keys, strict dotenv parsing and tracked-input refusal. |
| Chart dependencies/lint | `helm dependency build helm/inference`; `helm lint helm/inference`; `helm lint helm/orchestrator --values helm/orchestrator/example.values.yaml` |
| Chart rendering | `helm template inference helm/inference -n inference`; `helm template orchestrator helm/orchestrator -n orchestrator -f helm/orchestrator/example.values.yaml`; run with synthetic settings only and do not log secret-bearing manifests. Verify schemas reject plaintext secrets and invalid values. |
| Effective production comparison | Restricted operator compares legacy vs pinned-package renders using approved nonsecret settings; assert resource names/selectors, ports/RBAC, storage, image digests, Secret refs and catalog hashes unchanged unless explicitly approved. Do not publish renders or read local files in this documentation task. |
| UI gates | In `orchestrator/management/ui`: `npm run lint`, `npm run test`, `npm run build`, `npm run test:e2e`; distinguish local Playwright mocks from real integrated acceptance. |
| Artifact/access gate | Authorized publishers/consumers prove chart push/fetch and image pulls across orgs, pinned digest resolution, node architecture, locked dependency availability and rollback artifact retention. No permission inferred from workflow YAML. |
| Documentation gate | Review relative links and ownership coverage; use editor diagnostics. No Markdown lint configuration was found in the inspected workspace; no CLI Markdown lint is claimed. |

Isolated integration acceptance must exercise real HTTPS/OIDC/App intake,
management login/task listing and submission, queue persistence, supervisor claim,
hash-validated pinned catalog load, worker Job with a pinned image, broker token
renewal, coder/judge router preparation and chat, judge result, branch/PR
publication and authenticated terminal callback. Verify task status persists
across gateway restart with one replica. Test duplicate intake, denied identity,
missing Secret, unavailable model and expired credential paths fail closed;
reconcile task/branch/PR side effects before any intentional retry.

For production acceptance, repeat an approved canary task in a designated test
repository, verify DNS/TLS, GPU readiness, existing model-cache persistence,
SQLite health and app/hardware monitoring continuity; record task ID and artifact
set without credentials. Require no stranded claims, duplicate publication or
catalog hash mismatch before reopening intake. Staging GPU capacity and external
services must be arranged; mocked/local tests do not establish these outcomes.

## Rollback And Stop Conditions

Rollback unit is the complete last-known-good release set: both chart artifacts,
four App images, upstream runtime, nonsecret config commit, compatible DB backup,
historical catalogs and valid credential-generation references. Reapply that set
through one approved deployment authority; do not rely on `helm rollback` when
the legacy wrapper retained only one revision. Keep PVCs/model caches and release
names intact. Do not run the old parallel wrapper to regain authority.

Close intake and reconcile workers before rollback. If new code wrote a schema
or task format old readers cannot consume, stop all writers and use the rehearsed
restore procedure only with explicit acceptance of post-backup data loss and
branch/PR reconciliation. Do not automatically restore a stale DB or revoked
credential. A security rotation can make a historical set unusable; prepare a
compatible set with valid credentials rather than weakening authentication.

Stop and escalate for unverified artifact access, secret/history findings,
unresolved dirty-source provenance, missing backup/restore evidence, active
unreconciled tasks, unsupported DB/catalog/credential combinations, ownership/PVC
collisions, invalid callbacks/TLS, GPU/model readiness failure, insufficient
capacity, or failed canary/monitoring. Leave intake closed when state integrity is
uncertain. Do not delete resources, rewrite snapshots or improvise a redesign.

## Decisions And Execution Boundary

Before execution, name approvers/operators for all three repos and decide:
repository existence/visibility and branch protections; authorized extraction
baseline/history strategy; chart registry paths and cross-org publish/read
permissions; retention and rollback window; production values review and Secret
custodians; staging capacity/network/GitHub App installations; backup location,
RPO/RTO and restore owner; unresolved live task/DNS/catalog incidents; maintenance
window, canary repository and exact cutover/rollback commands.

Approving this document approves a plan only. Repository creation/cloning,
extraction, workflow/config/code edits, secret provisioning/rotation, deployments,
task recovery, history rewriting, commits and pushes each require separately
authorized execution. This change performs none of those actions.