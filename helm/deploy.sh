#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh — deploys the two independent charts under helm/ into separate
# namespaces on the K3s cluster (k8s-node01).
#
# Layout:
#   helm/inference/    -> release inference      (namespace: inference)
#   helm/orchestrator/ -> release orchestrator (namespace: orchestrator)
#
# The nvidia-device-plugin sub-chart is bundled inside the inference chart but is
# deferred (gpu: "0") until the NVIDIA device-plugin can run on this K3s node
# (it requires NFD-detected feature labels). Install NFD + the device-plugin
# first, then set gpu: "1" and re-run.
# ---------------------------------------------------------------------------
set -euo pipefail

# --- config ----------------------------------------------------------------
# Resolve the script's directory robustly from $0 / BASH_SOURCE (NOT $PWD, which
# is the process cwd and can be the workspace root or anywhere else). This makes
# the chart paths ("inference"/"orchestrator") correct regardless of where the
# script is invoked from (./deploy.sh, bash deploy.sh, or an absolute path).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
# The chart dirs (inference/, orchestrator/) live directly under helm/, so the
# helm dir defaults to the script's own directory (the helm/ directory).
HELM_DIR="${HELM_DIR:-$SCRIPT_DIR}"
# Orchestrator-specific nonsecret settings and Secret references.
ORCHESTRATOR_VALUES_FILE="${ORCHESTRATOR_VALUES_FILE:-${HELM_DIR}/orchestrator/local.values.yaml}"
HELM_TIMEOUT="${HELM_TIMEOUT:-10m}"
NAMESPACE_IN="inference"
NAMESPACE_O="orchestrator"
RELEASE_IN="inference"
RELEASE_O="orchestrator"


# --- helpers ---------------------------------------------------------------
log()  { printf '==> %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }

# Install or upgrade the release into its namespace. `upgrade --install` is
# idempotent: it creates the namespace on a fresh cluster and overwrites the
# release on a re-run, so it is safe on both a bare cluster and a re-deploy and
# avoids the "namespace already exists" race that a manual delete+install sequence
# triggers on K3s (K3s keeps the namespace briefly while pods drain).
deploy_release() {
  # Pre-create the namespace with kubectl BEFORE helm. helm's `--create-namespace`
  # uses a server-apply that K3s does not always reflect back immediately, so helm
  # can still see "namespace already exists" (or "not found") on re-runs; creating
  # it explicitly up front avoids that race deterministically.
  kubectl create namespace "$1" >/dev/null 2>&1 || true
  # `--install` so it creates the release on a fresh cluster and overwrites on a
  # re-run.
  local -a values_args=()
  local -a reset_values_args=()
  if [[ "$3" == "orchestrator" && -f "${ORCHESTRATOR_VALUES_FILE}" ]]; then
    values_args+=(--values "${ORCHESTRATOR_VALUES_FILE}")
  fi
  if [[ "$3" == "inference" ]]; then
    reset_values_args+=(--reset-values)
  fi
  # Delete the controller-owned Job before the upgrade. Helm's strategic-merge
  # patch on a Job conflicts with the immutable fields the controller stamps on
  # the pod template (batch.kubernetes.io/controller-uid,
  # batch.kubernetes.io/job-name, controller-uid), which surfaces as
  # "field is immutable". Removing the Job first lets it be recreated cleanly on
  # the next deploy. The Job name is "<release>-worker" (e.g. orchestrator-worker).
  kubectl delete job "${2}-worker" -n "$1" >/dev/null 2>&1 || true
  if helm upgrade --install "${reset_values_args[@]}" --namespace "$1" "$2" "$HELM_DIR/$3" "${values_args[@]}" --wait --timeout "$HELM_TIMEOUT" --history-max 1; then
    log "release '$2' ready in namespace '$1'"
  else
    local upgrade_status=$?
    warn "Helm upgrade/install failed for release '$2'"
    kubectl get pods -n "$1" -o wide >&2 || true
    kubectl get events -n "$1" --sort-by=.metadata.creationTimestamp >&2 || true
    return "$upgrade_status"
  fi
}

# --- preflight ------------------------------------------------------------
command -v helm >/dev/null 2>&1 || { warn "helm not found"; exit 1; }
command -v kubectl >/dev/null 2>&1 || { warn "kubectl not found"; exit 1; }
command -v jq >/dev/null 2>&1 || { warn "jq not found"; exit 1; }
command -v python3 >/dev/null 2>&1 || { warn "python3 not found"; exit 1; }
log "helm dir:  $HELM_DIR"
log "node:      $(kubectl get node -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo 'unknown')"

# Ensure dependency charts are present (notably nvidia-device-plugin under
# inference/charts) so `helm upgrade --install` does not fail on missing deps.
if [[ -f "$HELM_DIR/inference/Chart.yaml" ]]; then
  log "build inference chart dependencies";
  helm dependency build "$HELM_DIR/inference" >/dev/null
fi

validate_chart_secret_refs() {
  local namespace="$1"
  local release="$2"
  local chart="$3"
  local -a values_args=()
  if [[ "$chart" == "orchestrator" && -f "$ORCHESTRATOR_VALUES_FILE" ]]; then
    values_args+=(--values "$ORCHESTRATOR_VALUES_FILE")
  fi
  python3 "$SCRIPT_DIR/validate_secret_refs.py" \
    --chart "$HELM_DIR/$chart" --release "$release" --namespace "$namespace" \
    "${values_args[@]}"
}

validate_release() {
  local namespace="$1"
  local release="$2"
  local release_info
  local workloads

  if ! release_info="$(helm status "$release" --namespace "$namespace" --output json)"; then
    warn "could not read Helm status for '$release' in namespace '$namespace'"
    return 1
  fi
  if ! jq -e '.info.status == "deployed"' <<<"$release_info" >/dev/null; then
    warn "Helm release '$release' is not deployed"
    return 1
  fi
  if ! workloads="$(kubectl get deployments,statefulsets,daemonsets -n "$namespace" -o json)"; then
    warn "could not read workload status in namespace '$namespace'"
    return 1
  fi
  if ! jq -e '
    [.items[] | {
      kind,
      name: .metadata.name,
      desired: (if .kind == "DaemonSet" then (.status.desiredNumberScheduled // 0) else (.spec.replicas // 1) end),
      ready: (if .kind == "DaemonSet" then (.status.numberReady // 0) elif .kind == "StatefulSet" then (.status.readyReplicas // 0) else (.status.availableReplicas // 0) end)
    }] as $workloads
    | ($workloads | length) > 0
      and all($workloads[]; .ready >= .desired and (.kind != "DaemonSet" or .desired > 0))
  ' <<<"$workloads" >/dev/null; then
    warn "one or more workloads are not ready in namespace '$namespace'"
    kubectl get deployments,statefulsets,daemonsets -n "$namespace" -o wide >&2 || true
    return 1
  fi
  log "release '$release' and workloads ready in namespace '$namespace'"
}

kubectl create namespace "$NAMESPACE_IN" >/dev/null 2>&1 || kubectl get namespace "$NAMESPACE_IN" >/dev/null
kubectl create namespace "$NAMESPACE_O" >/dev/null 2>&1 || kubectl get namespace "$NAMESPACE_O" >/dev/null
log "validate external Secret references before deployment"
SECRET_PREFLIGHT_STATUS=0
if validate_chart_secret_refs "$NAMESPACE_IN" "$RELEASE_IN" "inference"; then
  :
else
  warn "inference Secret preflight failed"
  SECRET_PREFLIGHT_STATUS=1
fi
if validate_chart_secret_refs "$NAMESPACE_O" "$RELEASE_O" "orchestrator"; then
  :
else
  warn "orchestrator Secret preflight failed"
  SECRET_PREFLIGHT_STATUS=1
fi
if [[ "$SECRET_PREFLIGHT_STATUS" -ne 0 ]]; then
  warn "provision the listed Secrets and keys before deploying; no releases were upgraded"
  exit "$SECRET_PREFLIGHT_STATUS"
fi

# --- deploy charts in parallel --------------------------------------------
log "deploy inference chart into namespace '$NAMESPACE_IN'";
deploy_release "$NAMESPACE_IN" "$RELEASE_IN" "inference" &
INFERENCE_PID=$!

log "deploy orchestrator chart into namespace '$NAMESPACE_O'";
deploy_release "$NAMESPACE_O" "$RELEASE_O" "orchestrator" &
ORCHESTRATOR_PID=$!

DEPLOY_STATUS=0
if wait "$INFERENCE_PID"; then
  :
else
  INFERENCE_STATUS=$?
  warn "inference release deployment failed (status: $INFERENCE_STATUS)"
  DEPLOY_STATUS=1
fi
if wait "$ORCHESTRATOR_PID"; then
  :
else
  ORCHESTRATOR_STATUS=$?
  warn "orchestrator release deployment failed (status: $ORCHESTRATOR_STATUS)"
  DEPLOY_STATUS=1
fi

if validate_release "$NAMESPACE_IN" "$RELEASE_IN"; then
  :
else
  DEPLOY_STATUS=1
fi
if validate_release "$NAMESPACE_O" "$RELEASE_O"; then
  :
else
  DEPLOY_STATUS=1
fi
if [[ "$DEPLOY_STATUS" -ne 0 ]]; then
  exit "$DEPLOY_STATUS"
fi

# --- summary --------------------------------------------------------------
log "deployments ready. pods:"
kubectl get pods -n "$NAMESPACE_IN"   --show-labels=false 2>/dev/null | tail -n +2
kubectl get pods -n "$NAMESPACE_O"    --show-labels=false 2>/dev/null | tail -n +2
# log "next: install NFD, then set gpu: \"1\" in helm/inference/values.yaml and re-run this script."

kubectl -n "$NAMESPACE_O" delete pods --field-selector=status.phase=Succeeded >/dev/null
kubectl -n "$NAMESPACE_O" delete pods --field-selector=status.phase=Failed >/dev/null
