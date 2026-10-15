# Quick Setup: Add a New Repository

**TL;DR:** Copy-paste steps to enable orchestrator on a new repo.

---

## Step 1: Add Workflow File

Copy this to your repository at `.github/workflows/orchestrator-drain.yaml`:

```yaml
name: Send to Orchestrator

on:
  issues:
    types: [opened, labeled]
  pull_request:
    types: [opened, synchronize]
  workflow_dispatch:

jobs:
  send-to-orchestrator:
    runs-on: ubuntu-latest
    steps:
      - name: Extract task info
        id: task-info
        run: |
          if [ "${{ github.event_name }}" = "issues" ]; then
            TASK_ID="${{ github.event.issue.number }}"
            ISSUE_TITLE="${{ github.event.issue.title }}"
            ISSUE_URL="${{ github.event.issue.html_url }}"
          elif [ "${{ github.event_name }}" = "pull_request" ]; then
            TASK_ID="${{ github.event.pull_request.number }}"
            ISSUE_TITLE="${{ github.event.pull_request.title }}"
            ISSUE_URL="${{ github.event.pull_request.html_url }}"
          else
            exit 1
          fi
          echo "task_id=$TASK_ID" >> $GITHUB_OUTPUT
          echo "issue_title=$ISSUE_TITLE" >> $GITHUB_OUTPUT
          echo "issue_url=$ISSUE_URL" >> $GITHUB_OUTPUT
      
      - name: Send task to orchestrator
        env:
          ORCHESTRATOR_URL: ${{ secrets.ORCHESTRATOR_URL }}
          ORCHESTRATOR_TOKEN: ${{ secrets.ORCHESTRATOR_TOKEN }}
        run: |
          curl -X POST "$ORCHESTRATOR_URL" \
            -H "Authorization: token $ORCHESTRATOR_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{
              \"task_id\": \"${{ steps.task-info.outputs.task_id }}\",
              \"repo\": \"${{ github.repository }}\",
              \"issue_number\": ${{ steps.task-info.outputs.task_id }},
              \"issue_title\": \"${{ steps.task-info.outputs.issue_title }}\",
              \"issue_url\": \"${{ steps.task-info.outputs.issue_url }}\"
            }"
```

---

## Step 2: Add GitHub Secrets

Go to: **Repository Settings → Secrets and variables → Actions**

Add two secrets:

### Secret 1: `ORCHESTRATOR_URL`
```
http://orchestrator.local/api/v1/tasks/spawn
```

This URL is exposed through Traefik on the `web` entrypoint (port 80).

### Secret 2: `ORCHESTRATOR_TOKEN`
```
<your-GitHub-PAT-with-repo-scope>
```

To create a PAT:
- GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
- Click "Generate new token"
- Name: `orchestrator`
- Scopes: `repo` (full control of private repositories)
- Copy the token and paste as `ORCHESTRATOR_TOKEN` secret

---

## Step 3: Test

Create an issue or open a PR on the repo → workflow runs automatically.

Check the workflow run:
- Go to **Actions** tab on the repo
- Click **Send to Orchestrator** workflow
- View run logs

Expected success log:
```
✓ Task accepted by orchestrator
```

---

## Troubleshooting

**Workflow fails with 401 Unauthorized:**
- Check `ORCHESTRATOR_TOKEN` is set and not expired
- Verify the token has `repo` scope

**Workflow fails with 403 Forbidden:**
- Repo name doesn't match `ALLOWED_REPOS` setting on orchestrator
- (Currently set to `*` = accept all repos)

**Workflow fails with connection refused:**
- Orchestrator service is not running
- Check: `kubectl get svc -n orchestrator`
- Check: `kubectl get ingress -n orchestrator`

---

## Verify Orchestrator Configuration

Check what repos are allowed:

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get deployment k8s-inference-orchestrator -n orchestrator -o jsonpath='{.spec.template.spec.containers[1].env[?(@.name=="ALLOWED_REPOS")].value}'
```

Current setting:
- `*` = Accept from ANY repo
- `repo1,repo2,repo3` = Whitelist only these repos

To change:
```bash
kubectl set env deployment/k8s-inference-orchestrator \
  -n orchestrator \
  ALLOWED_REPOS="org/repo1,org/repo2,org/repo3"
```

---

## That's it!

The repo is now sending tasks to the orchestrator. Check the supervisor logs:

```bash
kubectl logs -n orchestrator -l app=orchestrator -c task-acceptor -f
```

You'll see each task arrive:
```
INFO:task-acceptor:Task accepted: owner/repo#123
```
