# common.sh — shared environment for the inference tooling on k8s-node01.
# Sourced by deploy.sh (and other scripts in this directory).
#
# Defaults match k8s-node01.md. Override by exporting the variable before
# sourcing, e.g. `KUBECONFIG=/path/to/kubeconfig source common.sh`.

export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

# Container runtime image registry. K3s runs containerd; pull images with
# `ctr`/`nerdctl` (nerdctl is a convenient containerd client) into the
# containerd content store so the node's scheduler can pull them.
export IMAGE_REGISTRY="${IMAGE_REGISTRY:-ghcr.io}"

# Image pull secrets / registries the engine images need.
export REGISTRY_ARGS="${REGISTRY_ARGS:-}"

# Where weights are mounted inside the server pod.
export MODEL_MOUNT_PATH="${MODEL_MOUNT_PATH:-/models}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# Pull a containerd image into the node's store (nerdctl if present, else ctr).
pull_image() {
  local image="$1"; shift
  log "Pulling containerd image: ${image}"
  if command -v nerdctl >/dev/null 2>&1; then
    nerdctl pull "${REGISTRY_ARGS:+$REGISTRY_ARGS}" "${image}"
  elif command -v ctr >/dev/null 2>&1; then
    ctr image pull "${REGISTRY_ARGS:+$REGISTRY_ARGS}" "${image}"
  else
    echo "WARNING: neither nerdctl nor ctr found; run containerd image pull manually"
  fi
}

# Push the images the stack needs into containerd (best-effort).
ensure_images() {
  local images=(
    "nvcr.io/nvidia/k8s-device-plugin:2.18.0"
    "nvcr.io/nvidia/k8s-node-problem-detector:0.18.0"
    "${IMAGE_REGISTRY}/ggerganov/llama.cpp:latest"
  )
  local img
  for img in "${images[@]}"; do
    pull_image "${img}"
  done
}
