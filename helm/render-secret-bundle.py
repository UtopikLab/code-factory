"""Render a strict local dotenv file into external Kubernetes resources."""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

REQUIRED_KEYS = {
    "GHCR_USERNAME",
    "GHCR_READ_PACKAGES_PAT",
    "GITHUB_APP_PRIVATE_KEY",
    "SUPERVISOR_AUTH_TOKEN",
    "MANAGEMENT_SERVICE_TOKEN",
    "MANAGEMENT_ADMIN_PASSWORD",
}
OPTIONAL_KEYS = {
    "OPENAI_API_KEY",
    "CODER_API_KEY",
    "MANAGEMENT_GITHUB_CLIENT_SECRET",
}
ALL_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS
ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def parse_dotenv(text):
    """Parse simple dotenv assignments without shell expansion or execution."""
    values = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match:
            raise ValueError(f"invalid dotenv assignment on line {line_number}")
        key, encoded = match.groups()
        if key in values:
            raise ValueError(f"duplicate dotenv key on line {line_number}")
        if not encoded.startswith('"'):
            value = encoded.strip()
            if '"' in value or "'" in value or "\\" in value:
                raise ValueError(f"invalid unquoted value on line {line_number}")
            values[key] = value
            continue
        if len(encoded) < 2 or not encoded.endswith('"'):
            raise ValueError(f"invalid quoted value on line {line_number}")
        body = encoded[1:-1]
        value = []
        index = 0
        while index < len(body):
            char = body[index]
            if char == "\\":
                index += 1
                if index == len(body):
                    raise ValueError(f"invalid escape on line {line_number}")
                escaped = body[index]
                escape_values = {"\\": "\\", '"': '"', "n": "\n", "r": "\r"}
                if escaped not in escape_values:
                    raise ValueError(f"unsupported escape on line {line_number}")
                value.append(escape_values[escaped])
            elif char == '"':
                raise ValueError(f"unescaped quote on line {line_number}")
            else:
                value.append(char)
            index += 1
        values[key] = "".join(value)
    return values


def merge_values(base, overrides):
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_values(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_values(paths):
    values = {}
    for path in paths:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            raise ValueError(f"could not read valid chart values from {path}") from None
        if not isinstance(loaded, dict):
            raise TypeError(f"chart values must be a mapping: {path}")
        values = merge_values(values, loaded)
    return values


def validate_secret_name(name, source):
    if not isinstance(name, str) or not name:
        raise ValueError(f"{source} is empty in chart values")


def add_secret(secrets, namespace, name, secret_type, data, source):
    validate_secret_name(name, source)
    if not data or any(not key or not isinstance(value, str) for key, value in data.items()):
        raise ValueError(f"Secret {namespace}/{name} has invalid data")
    identity = (namespace, name)
    if identity in secrets:
        raise ValueError(f"duplicate Secret target {namespace}/{name}")
    secrets[identity] = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": secret_type,
        "stringData": data,
    }


def configured_ref(values, ref_name):
    ref = (values.get("secretRefs") or {}).get(ref_name) or {}
    if not isinstance(ref, dict):
        raise TypeError(f"secretRefs.{ref_name} must be a mapping")
    return ref.get("name", ""), ref.get("key", "")


def render(bundle, inference_values, orchestrator_values):
    if not isinstance(bundle, dict) or any(not isinstance(value, str) for value in bundle.values()):
        raise ValueError("decrypted dotenv data must be a mapping of string values")
    unknown = set(bundle) - ALL_KEYS
    missing = REQUIRED_KEYS - set(bundle)
    empty = {key for key in REQUIRED_KEYS if not bundle.get(key, "").strip()}
    if unknown:
        raise ValueError("unknown dotenv keys: " + ", ".join(sorted(unknown)))
    if missing:
        raise ValueError("missing required dotenv keys: " + ", ".join(sorted(missing)))
    if empty:
        raise ValueError("required dotenv values are empty: " + ", ".join(sorted(empty)))

    docker_config = json.dumps({"auths": {"ghcr.io": {
        "username": bundle["GHCR_USERNAME"],
        "password": bundle["GHCR_READ_PACKAGES_PAT"],
    }}}, separators=(",", ":"))
    secrets = {}
    inference_namespace = "inference"
    orchestrator_namespace = "orchestrator"
    if inference_values.get("namespace", inference_namespace) != inference_namespace:
        raise ValueError("inference namespace must match helm/deploy.sh")
    if orchestrator_values.get("namespace", orchestrator_namespace) != orchestrator_namespace:
        raise ValueError("orchestrator namespace must match helm/deploy.sh")
    namespaces = {inference_namespace, orchestrator_namespace}
    documents = [{
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace},
    } for namespace in sorted(namespaces)]
    pull_names = {}
    pull_secret_groups = (
        (inference_namespace, "inference.imagePullSecrets", (inference_values.get("inference") or {}).get("imagePullSecrets", [])),
        (orchestrator_namespace, "imagePullSecrets", orchestrator_values.get("imagePullSecrets", [])),
    )
    for namespace, source, pull_secrets in pull_secret_groups:
        for index, item in enumerate(pull_secrets):
            item_source = f"{source}[{index}].name"
            name = item.get("name")
            validate_secret_name(name, item_source)
            pull_names.setdefault((namespace, name), item_source)
    for (namespace, name), source in sorted(pull_names.items()):
        add_secret(secrets, namespace, name, "kubernetes.io/dockerconfigjson", {
            ".dockerconfigjson": docker_config,
        }, source)

    fixed_refs = (
        ("githubAppKey", "GITHUB_APP_PRIVATE_KEY"),
        ("supervisorServiceToken", "SUPERVISOR_AUTH_TOKEN"),
        ("managementServiceToken", "MANAGEMENT_SERVICE_TOKEN"),
        ("managementAdminPassword", "MANAGEMENT_ADMIN_PASSWORD"),
    )
    for ref_name, env_key in fixed_refs:
        name, key = configured_ref(orchestrator_values, ref_name)
        value = bundle[env_key]
        if ref_name == "githubAppKey":
            value = value.replace("\\n", "\n").rstrip("\n") + "\n"
        add_secret(
            secrets,
            orchestrator_namespace,
            name,
            "Opaque",
            {key: value},
            f"secretRefs.{ref_name}.name",
        )

    optional_refs = (
        ("openaiApiKey", "OPENAI_API_KEY"),
        ("coderApiKey", "CODER_API_KEY"),
        ("managementGithubClientSecret", "MANAGEMENT_GITHUB_CLIENT_SECRET"),
    )
    for ref_name, env_key in optional_refs:
        value = bundle.get(env_key, "")
        if not value:
            continue
        name, key = configured_ref(orchestrator_values, ref_name)
        add_secret(
            secrets,
            orchestrator_namespace,
            name,
            "Opaque",
            {key: value},
            f"secretRefs.{ref_name}.name",
        )

    return documents + list(secrets.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dotenv-file", required=True, type=Path)
    parser.add_argument("--inference-values", required=True, type=Path)
    parser.add_argument("--orchestrator-values", required=True, type=Path)
    parser.add_argument("--orchestrator-overrides", type=Path)
    args = parser.parse_args()
    try:
        bundle = parse_dotenv(args.dotenv_file.read_text(encoding="utf-8"))
        inference_values = load_values([args.inference_values])
        orchestrator_paths = [args.orchestrator_values]
        if args.orchestrator_overrides and args.orchestrator_overrides.is_file():
            orchestrator_paths.append(args.orchestrator_overrides)
        orchestrator_values = load_values(orchestrator_paths)
        documents = render(bundle, inference_values, orchestrator_values)
        sys.stdout.write(yaml.safe_dump_all(documents, explicit_start=True, sort_keys=False))
    except (OSError, TypeError, ValueError) as error:
        print(f"Secret bundle error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())