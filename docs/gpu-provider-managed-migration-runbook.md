# GPU Infra Migration Runbook (Custom Chart -> Provider-Managed)

## Scope

This runbook migrates GPU infrastructure ownership (GPU namespace/RBAC/device-plugin/node-problem-detector) from this repo's chart to a trusted provider-managed chart, while continuing to deploy the application stack from this repo.

Repository chart path:
- `./helm`

Key value switch in this repo:
- `gpu.externalManaged` (default: `false`)

Behavior:
- `gpu.externalManaged=false`: current behavior, this chart manages GPU infra.
- `gpu.externalManaged=true`: this chart stops rendering GPU infra resources.

Note:
- The `gpu.devicePlugin.image` value remains unchanged in this repo.
- `gpu.nodeProblemDetector.enabled` is optional and disabled by default.

## Prerequisites

1. Working kube context for target cluster.
2. Helm 3 installed.
3. Provider GPU chart access and values file prepared.
4. If using NVIDIA NPD image from NVCR, ensure valid entitlement/pull credentials.

## Phase 0 - Baseline Validation (Current Custom Mode)

Run from repo root:

```bash
cd /home/user/code-factory
helm lint ./helm
helm template code-factory ./helm > /tmp/code-factory-render.yaml
```

Snapshot current GPU infra state:

```bash
kubectl get ns gpu
kubectl -n gpu get ds
kubectl -n gpu get pods -o wide
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPUS:.status.allocatable.nvidia\.com/gpu
```

## Phase 1 - Install/Upgrade Provider GPU Chart

Example commands (replace placeholders with your provider chart details):

```bash
helm repo add <provider-repo-name> <provider-repo-url>
helm repo update
helm upgrade --install gpu-provider <provider-repo-name>/<provider-gpu-chart> \
  --namespace gpu \
  --create-namespace \
  -f <provider-values.yaml>
```

Verify provider GPU components are healthy before moving app chart ownership:

```bash
kubectl -n gpu get ds
kubectl -n gpu get pods -o wide
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPUS:.status.allocatable.nvidia\.com/gpu
```

## Phase 2 - Flip App Chart to External-Managed GPU Infra

Dry run first:

```bash
helm template code-factory ./helm \
  --set gpu.externalManaged=true > /tmp/code-factory-external-gpu-render.yaml
```

Apply upgrade for this repo chart:

```bash
helm upgrade --install code-factory ./helm \
  --namespace inference \
  --create-namespace \
  --set gpu.externalManaged=true
```

If you use a values file, prefer:

```bash
helm upgrade --install code-factory ./helm \
  --namespace inference \
  --create-namespace \
  -f <your-values.yaml> \
  --set gpu.externalManaged=true
```

## Phase 3 - Verification Checks

1. GPU infra ownership/state:

```bash
kubectl -n gpu get ds
kubectl -n gpu get pods -o wide
```

2. Node GPU capacity present:

```bash
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPUS:.status.allocatable.nvidia\.com/gpu
```

3. App workloads still schedule and run with GPU requests:

```bash
kubectl -n inference get deploy,pods -o wide
kubectl -n inference describe pod -l app=inference-server | grep -E "nvidia.com/gpu|Node:"
```

4. Smoke test inference endpoint:

```bash
kubectl -n inference get svc
curl -sS http://inference.local/v1/models | head -c 500
```

## Rollback

### Rollback A - Return App Chart to Custom GPU Infra Ownership

```bash
helm upgrade --install code-factory ./helm \
  --namespace inference \
  --create-namespace \
  --set gpu.externalManaged=false
```

### Rollback B - Roll Back Provider GPU Chart Revision

```bash
helm -n gpu history gpu-provider
helm -n gpu rollback gpu-provider <REVISION>
```

### Rollback C - Uninstall Provider GPU Chart (if needed)

```bash
helm -n gpu uninstall gpu-provider
```

If provider chart is removed, re-apply app chart in custom mode immediately:

```bash
helm upgrade --install code-factory ./helm \
  --namespace inference \
  --create-namespace \
  --set gpu.externalManaged=false
```

## NVCR Entitlement Note (Node Problem Detector)

If enabling `gpu.nodeProblemDetector.enabled=true`, the image path is NVCR-based and may require NVIDIA entitlement and pull secrets for your environment. Keep it disabled unless entitlement and credentials are validated.
