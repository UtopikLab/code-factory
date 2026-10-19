#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh — deploys the two independent root-level charts into separate
# namespaces on the K3s cluster (k8s-node01).
#
# Layout:
#   helm/inference/    -> release k8s-inference   (namespace: inference)
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
NAMESPACE_IN="inference"
NAMESPACE_O="orchestrator"
RELEASE_IN="k8s-inference"
RELEASE_O="orchestrator"

# --- helpers ---------------------------------------------------------------
log()  { printf '==> %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }

# Install or upgrade the release into its namespace. `upgrade --install` is
# idempotent: it creates the namespace on a fresh cluster and overwrites the
# release on a re-run, so it is safe on both a bare cluster and a re-deploy and
# avoids the "namespace already exists" race that a manual delete+install sequence
# triggers on K3s (K3s keeps the namespace briefly while pods drain). `--wait=false`
# lets a not-yet-ready device-plugin (deferred GPU) not block the release; GPUs
# come online later via NFD.
reset_ns() {
  # Pre-create the namespace with kubectl BEFORE helm. helm's `--create-namespace`
  # uses a server-apply that K3s does not always reflect back immediately, so helm
  # can still see "namespace already exists" (or "not found") on re-runs; creating
  # it explicitly up front avoids that race deterministically.
  kubectl create namespace "$1" >/dev/null 2>&1 || true
  # `--install` so it creates the release on a fresh cluster and overwrites on a
  # re-run. `--wait=false` lets a not-yet-ready device-plugin (deferred GPU) not
  # block the release; GPUs come online later via NFD.
  helm upgrade --install --namespace "$1" "$2" "$HELM_DIR/$3" --wait=false || true
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
# log "next: install NFD, then set gpu: \"1\" in helm/inference/values.yaml and re-run this script."
