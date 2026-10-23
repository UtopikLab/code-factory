#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" || {
  printf '!! could not determine repository root\n' >&2
  exit 1
}
BUNDLE_FILE="${SCRIPT_DIR}/local.secret-bundle.env"
BUNDLE_RELATIVE="${BUNDLE_FILE#"$REPO_DIR"/}"

if git -C "$REPO_DIR" ls-files --error-unmatch -- "$BUNDLE_RELATIVE" >/dev/null 2>&1; then
  printf '!! local credential file is tracked; refusing to read it\n' >&2
  exit 1
fi
git -C "$REPO_DIR" check-ignore --quiet -- "$BUNDLE_RELATIVE" || {
  printf '!! local credential file is not ignored; refusing to read it\n' >&2
  exit 1
}
[[ -f "$BUNDLE_FILE" && ! -L "$BUNDLE_FILE" ]] || {
  printf '!! local credential file is missing or is not a regular file: helm/local.secret-bundle.env\n' >&2
  exit 1
}
[[ "$(stat -c '%u' "$BUNDLE_FILE")" == "$(id -u)" ]] || {
  printf '!! local credential file must be owned by the current user\n' >&2
  exit 1
}
chmod 600 "$BUNDLE_FILE" || {
  printf '!! could not restrict local credential file permissions\n' >&2
  exit 1
}

command -v kubectl >/dev/null 2>&1 || { printf '!! kubectl not found\n' >&2; exit 1; }

if python3 "$SCRIPT_DIR/render-secret-bundle.py" \
      --dotenv-file "$BUNDLE_FILE" \
      --inference-values "$SCRIPT_DIR/inference/values.yaml" \
      --orchestrator-values "$SCRIPT_DIR/orchestrator/values.yaml" \
      --orchestrator-overrides "${ORCHESTRATOR_VALUES_FILE:-$SCRIPT_DIR/orchestrator/local.values.yaml}" \
  | kubectl apply --filename - >/dev/null 2>/dev/null; then
  printf '==> External Secrets and namespaces applied; run ./helm/deploy.sh separately when authorized\n'
else
  printf '!! local Secret input failed validation or apply; no values were printed\n' >&2
  exit 1
fi
