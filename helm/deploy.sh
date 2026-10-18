#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh — deploys the two independent root-level charts into separate
# namespaces on the K3s cluster (k8s-node01).
#
# Layout:
#   helm/inference/    -> release k8s-inference   (namespace: inference)
#   helm/orchestrator/ -> release k8s-orchestrator (namespace: orchestrator)
#
# The nvidia-device-plugin sub-chart is bundled inside the inference chart but is
# deferred (gpu: "0") until the NVIDIA device-plugin can run on this K3s node
# (it requires NFD-detected feature labels). Install NFD + the device-plugin
# first, then set gpu: "1" and re-run.
# ---------------------------------------------------------------------------
set -euo pipefail

# --- config ----------------------------------------------------------------
# Resolve the script's directory robustly. $PWD is the process cwd, which matches
# the terminal's cwd (this tool does not preserve the working directory). Prefer it
# for the common "./deploy.sh", "bash deploy.sh", and absolute invocations; fall
# back to dirname($0) only for absolute-path invocations started elsewhere.
if [[ -n "${PWD:-}" ]]; then
  SCRIPT_DIR="$PWD"
else
  SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
fi
# The chart dirs (inference/, orchestrator/) live in the same directory as this
# script, so the helm dir defaults to the script's own directory.
HELM_DIR="${HELM_DIR:-$SCRIPT_DIR}"
NAMESPACE_IN="inference"
NAMESPACE_O="orchestrator"
RELEASE_IN="k8s-inference"
RELEASE_O="k8s-orchestrator"

# --- helpers ---------------------------------------------------------------
log()  { printf '==> %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }

# Install or upgrade the release into its namespace. On a fresh namespace there is
# no existing release, so use `install`; on a re-run use `upgrade` (which also
# overwrites). `--create-namespace` makes it safe on a bare cluster.
reset_ns() {
  local ns="$1"
  if kubectl get namespace "$ns" >/dev/null 2>&1; then
    kubectl delete namespace "$ns"
  fi
  # `--wait=false` so a not-yet-ready device-plugin (deferred GPU) doesn't block the
  # release; GPUs come online later via NFD.
  helm install --namespace "$ns" --create-namespace "$2" "$HELM_DIR/$3" --wait=false || true
}

# --- preflight ------------------------------------------------------------
command -v helm >/dev/null 2>&1 || { warn "helm not found"; exit 1; }
command -v kubectl >/dev/null 2>&1 || { warn "kubectl not found"; exit 1; }
log "helm dir:  $HELM_DIR"
log "node:      $(kubectl get node -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo 'unknown')"

# --- deploy inference ------------------------------------------------------
log "deploy inference sub-chart into namespace '$NAMESPACE_IN'";
reset_ns "$NAMESPACE_IN" "$RELEASE_IN" "inference";

# --- deploy orchestrator --------------------------------------------------
log "deploy orchestrator sub-chart into namespace '$NAMESPACE_O'";
reset_ns "$NAMESPACE_O" "$RELEASE_O" "orchestrator";

# --- summary --------------------------------------------------------------
log "done. releases:"
kubectl get pods -n "$NAMESPACE_IN"   --show-labels=false 2>/dev/null | tail -n +2
kubectl get pods -n "$NAMESPACE_O"    --show-labels=false 2>/dev/null | tail -n +2
log "next: install NFD, then set gpu: \"1\" in helm/inference/values.yaml and re-run this script."
