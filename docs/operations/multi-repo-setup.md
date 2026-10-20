# Multi-Repo Orchestrator Configuration

Push tasks from **any GitHub repository** to the k8s-node01 orchestrator.

---

## Quick Start

### Step 1: Update Orchestrator to Accept Multiple Repos

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# Option A: Accept tasks from ANY repo
kubectl set env deployment/k8s-inference-orchestrator \
  -n orchestrator \
  ALLOWED_REPOS="*"

# Option B: Whitelist specific repos
kubectl set env deployment/k8s-inference-orchestrator \
  -n orchestrator \
  ALLOWED_REPOS="org/repo1,org/repo2,MyOrg/another-repo"
```

Verify the change:
```bash
kubectl rollout status deployment/k8s-inference-orchestrator -n orchestrator --timeout=60s
```

### Step 2: On Each Repository, Create GitHub Actions Workflow

Create `.github/workflows/orchestrator-drain.yaml`:

```yaml
name: Send to Orchestrator

on:
  issues:
    types: [opened, labeled]
  pull_request:
    types: [opened]
  workflow_dispatch:

jobs:
  send-to-orchestrator:
    runs-on: ubuntu-latest
    steps:
      - name: Send task to orchestrator
        run: |
          TASK_ID="${{ github.event.issue.number || github.event.pull_request.number }}"
          ISSUE_TITLE="${{ github.event.issue.title || github.event.pull_request.title }}"
          ISSUE_URL="${{ github.event.issue.html_url || github.event.pull_request.html_url }}"
          
          curl -X POST "${{ secrets.ORCHESTRATOR_URL }}" \
            -H "Authorization: token ${{ secrets.ORCHESTRATOR_TOKEN }}" \
            -H "Content-Type: application/json" \
            -d @- <<EOF
          {
            "task_id": "$TASK_ID",
            "repo": "${{ github.repository }}",
            "issue_number": $TASK_ID,
            "issue_title": "$ISSUE_TITLE",
            "issue_url": "$ISSUE_URL"
          }
          EOF
```

### Step 3: Add GitHub Secrets to Each Repo

For each repository, add two secrets in:  
**Settings → Secrets and variables → Actions**

| Secret | Value | Notes |
|---|---|---|
| `ORCHESTRATOR_URL` | `http://orchestrator.local/api/v1/tasks/spawn` | Traefik `web` entrypoint on port 80 |
| `ORCHESTRATOR_TOKEN` | Your GitHub PAT (repo scope minimum) | Same PAT used for `GITHUB_TOKEN` in orchestrator secrets |

#### Create the GitHub PAT:
```bash
# GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
# Scopes: repo (full control of private repositories)
# Name: orchestrator-webhook
# Save the token as ORCHESTRATOR_TOKEN in each repo
```

### Step 4: Trigger a Task

**Option A: Automatic (on issue/PR)**
- Create an issue on the repo → workflow runs automatically
- Or open a PR → workflow runs on every PR

**Option B: Manual workflow dispatch**
```bash
gh workflow run orchestrator-drain.yaml -R org/repo-name
```

**Option C: Test with curl**
```bash
curl -X POST http://k8s-inference-orchestrator.orchestrator.svc.cluster.local:8000/api/v1/tasks/spawn \
  -H "Authorization: token <ORCHESTRATOR_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "test-001",
    "repo": "org/repo-name",
    "issue_number": 123,
    "issue_title": "Fix: something broken",
    "issue_url": "https://github.com/org/repo-name/issues/123"
  }'
```

Expected response (HTTP 202):
```json
{
  "status": "accepted",
  "task_id": "test-001",
  "repo": "org/repo-name",
  "issue_number": 123,
  "endpoint": "/api/v1/tasks/spawn"
}
```

---

## Configuration Options

### ALLOWED_REPOS Environment Variable

Set on the orchestrator deployment:

| Value | Meaning | Example |
|---|---|---|
| `*` | Accept from ANY repository (no validation) | `ALLOWED_REPOS="*"` |
| Single repo | Only this repo can send tasks | `ALLOWED_REPOS="UtopikLab/code-factory"` |
| Multiple repos (comma-separated) | Whitelist specific repos | `ALLOWED_REPOS="org/repo1,org/repo2,other-org/repo3"` |

Update the deployment:
```bash
kubectl set env deployment/k8s-inference-orchestrator \
  -n orchestrator \
  ALLOWED_REPOS="<value>"
```

---

## Workflow Configuration Options

### Trigger on Issue Creation
```yaml
on:
  issues:
    types: [opened]
```

### Trigger on Issue with Specific Label
```yaml
on:
  issues:
    types: [opened, labeled]

jobs:
  send-to-orchestrator:
    if: contains(github.event.issue.labels.*.name, 'orchestrator')
    # ... rest of job
```

### Trigger on Every Pull Request
```yaml
on:
  pull_request:
    types: [opened, synchronize]
```

### Trigger on Manual Dispatch
```yaml
on:
  workflow_dispatch:
    inputs:
      issue_number:
        description: 'Issue number'
        required: true
```

### Conditional: Only Run for Specific Branches
```yaml
jobs:
  send-to-orchestrator:
    if: github.event.pull_request.base.ref == 'main'
    # ... rest of job
```

---

## Monitoring Tasks

### Watch orchestrator logs
```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl logs -n orchestrator -l app=orchestrator -c gateway -f
```

### Check task-acceptor health
```bash
kubectl exec -n orchestrator \
  $(kubectl get pods -n orchestrator -l app=orchestrator -o jsonpath='{.items[0].metadata.name}') \
  -c gateway -- \
  python3 -c "import http.client; c=http.client.HTTPConnection('localhost',8000); c.request('GET','/'); print(c.getresponse().read().decode())"
```

### View supervisor loop
```bash
kubectl logs -n orchestrator -l app=orchestrator -c supervisor -f
```

---

## Security Considerations

### Token Management
- Store `ORCHESTRATOR_TOKEN` as a GitHub secret (never hardcode)
- Use a dedicated PAT with minimal scope (repo access only)
- Rotate the PAT periodically

### Endpoint Security
- If using ingress: Enable TLS and require authentication
- For internal cluster: Use `ALLOWED_REPOS` whitelist instead of `*`
- Monitor task-acceptor logs for failed authentication attempts

### Task Payload Validation
- Orchestrator validates:
  - GitHub token matches (401 if not)
  - Repo is in whitelist (403 if not)
  - JSON payload is well-formed (400 if not)

---

## Troubleshooting

### Workflow fails with "401 Unauthorized"
**Check:**
- `ORCHESTRATOR_TOKEN` secret is set correctly
- Token has `repo` scope
- Token is not expired

### Workflow fails with "403 Forbidden"
**Check:**
- Repo name matches exactly: `owner/repo`
- Repo is in `ALLOWED_REPOS` list (or `ALLOWED_REPOS="*"`)

### Workflow fails with "Connection refused"
**Check:**
- Orchestrator service is running: `kubectl get svc -n orchestrator`
- Endpoint URL is correct (internal DNS vs public ingress)
- Network policy allows egress from GitHub Actions

### Task appears to be accepted but nothing happens
**Check:**
- Supervisor pod is running: `kubectl get pods -n orchestrator`
- Supervisor logs for errors: `kubectl logs -n orchestrator <pod> -c supervisor`
- Worker job template is valid: `kubectl get job -n orchestrator`

---

## Example: Multi-Org Setup

To accept tasks from multiple orgs/repos:

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

kubectl set env deployment/k8s-inference-orchestrator \
  -n orchestrator \
  ALLOWED_REPOS="UtopikLab/code-factory,MyCompany/backend,MyCompany/frontend,OpenSource/project"
```

Then on each repo's workflow, use the same `ORCHESTRATOR_URL` and `ORCHESTRATOR_TOKEN` secrets.

---

## Next Steps

1. **Apply to production:** `kubectl apply -f orchestrator-deployment-multi-repo.yaml`
2. **Test with one repo:** Create a simple issue and verify the workflow runs
3. **Add more repos:** Once confirmed, add the workflow file to other repos
4. **Monitor:** Watch logs as tasks arrive and are processed
5. **Tune:** Adjust `MAX_ITERATIONS` and `WORKER_COUNT` based on observed workload

---

**The orchestrator is now ready to accept tasks from any (or all) configured repositories.**
