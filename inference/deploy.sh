#!/usr/bin/env bash
# =============================================================================
# deploy.sh — install the llama.cpp inference engine on k8s-node01.
#
# Designed for the single-node K3s + containerd host described in
# k8s-node01.md. Applies the NVIDIA device-plugin (GPU discovery) and the
# llama.cpp server stack, and pulls the required containerd images.
#
# Usage:
#   sudo ./inference/deploy.sh                # dry-run
#   sudo ./inference/deploy.sh apply          # apply everything
#   sudo ./inference/deploy.sh remove         # tear the stack down
#
# Assumes:
#   - kubectl is on PATH and $KUBECONFIG points at the K3s control plane
#     (export KUBECONFIG=/etc/rancher/k3s/k3s.yaml, as in k8s-node01.md).
#   - The two P40s are already attached to the host and nvidia-smi works.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1090
source "${ROOT}/common.sh"   # shared env (KUBECONFIG, IMAGE_REGISTRY, etc.)

MODE="${1:-dry-run}"
echo "=== [${MODE}] llama.cpp inference deployment on k8s-node01 ==="

command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found"; exit 1; }
command -v nvidia-smi  >/dev/null 2>&1 || { echo "nvidia-smi not found — is NVIDIA installed?"; exit 1; }

apply() {
  echo "--> Applying GPU device-plugin (namespace 'gpu')..."
  kubectl apply -f "${ROOT}/nvidia-device-plugin.yaml"

  echo ">--> Applying llm stack (namespace 'llm')..."
  kubectl apply -f "${ROOT}/models-pvc.yaml"
  kubectl apply -f "${ROOT}/deployment.yaml"

  echo "--> Waiting for GPU pods / PVC..."
  kubectl wait --for=condition=ready pod -l app=nvidia-device-plugin -n gpu --timeout=120s || true
  kubectl -n llm wait --for=condition=ready pod --all --timeout=180s || true
}

remove() {
  echo "--> Removing llm stack..."
  kubectl delete -f "${ROOT}/deployment.yaml" >/dev/null
  kubectl delete -f "${ROOT}/models-pvc.yaml" >/dev/null
  echo ">--> Removing GPU device-plugin..."
  kubectl delete -f "${ROOT}/nvidia-device-plugin.yaml" >/dev/null
}

case "${MODE}" in
  dry-run)
    echo "DRY RUN — no cluster changes. Apply with: sudo ./inference/deploy.sh apply"
    ;;
  apply)   apply ;;
  remove)  remove ;;
  *) echo "unknown mode '${MODE}' (use dry-run|apply|remove)"; exit 1 ;;
esac

echo "=== done (${MODE}) ==="
echo "Check: kubectl get pods -A"
echo "Check: kubectl get pvc -n llm -o wide"
echo "Call:  https://llm.local/v1/chat/completions  (OpenAI-compatible)"
