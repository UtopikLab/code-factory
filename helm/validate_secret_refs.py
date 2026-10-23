"""Check rendered Kubernetes Secret references without exposing Secret data."""

import argparse
import subprocess
import sys
from pathlib import Path

import yaml


def _add_reference(references, namespace, name, required=True, key=None, secret_type=None):
    if not isinstance(name, str) or not name:
        raise ValueError(f"chart contains an empty Secret name in namespace {namespace}")
    identity = (namespace, name)
    reference = references.setdefault(identity, {
        "required": False,
        "keys": {},
        "types": set(),
    })
    reference["required"] = reference["required"] or required
    if key is not None:
        if not isinstance(key, str) or not key:
            raise ValueError(f"chart contains an empty Secret key for {namespace}/{name}")
        reference["keys"][key] = reference["keys"].get(key, False) or required
    if secret_type:
        reference["types"].add(secret_type)


def collect_secret_references(documents, default_namespace):
    """Collect Secret uses from rendered Kubernetes resources."""
    references = {}

    def visit(value, namespace):
        if isinstance(value, dict):
            key_ref = value.get("secretKeyRef")
            if isinstance(key_ref, dict):
                _add_reference(
                    references,
                    namespace,
                    key_ref.get("name"),
                    required=key_ref.get("optional") is not True,
                    key=key_ref.get("key"),
                )

            env_ref = value.get("secretRef")
            if isinstance(env_ref, dict):
                _add_reference(
                    references,
                    namespace,
                    env_ref.get("name"),
                    required=env_ref.get("optional") is not True,
                )

            for pull_secret in value.get("imagePullSecrets", []) or []:
                if isinstance(pull_secret, dict):
                    _add_reference(
                        references,
                        namespace,
                        pull_secret.get("name"),
                        key=".dockerconfigjson",
                        secret_type="kubernetes.io/dockerconfigjson",
                    )

            volume_secret = value.get("secret")
            if isinstance(volume_secret, dict) and "secretName" in volume_secret:
                name = volume_secret.get("secretName")
                items = volume_secret.get("items", []) or []
                if items:
                    for item in items:
                        if isinstance(item, dict):
                            _add_reference(references, namespace, name, key=item.get("key"))
                else:
                    _add_reference(references, namespace, name)

            for tls_secret in value.get("tls", []) or []:
                if isinstance(tls_secret, dict) and "secretName" in tls_secret:
                    name = tls_secret.get("secretName")
                    _add_reference(
                        references,
                        namespace,
                        name,
                        key="tls.crt",
                        secret_type="kubernetes.io/tls",
                    )
                    _add_reference(
                        references,
                        namespace,
                        name,
                        key="tls.key",
                        secret_type="kubernetes.io/tls",
                    )

            for child in value.values():
                visit(child, namespace)
        elif isinstance(value, list):
            for child in value:
                visit(child, namespace)

    for document in documents:
        if not isinstance(document, dict):
            continue
        metadata = document.get("metadata") or {}
        namespace = metadata.get("namespace", default_namespace)
        visit(document, namespace)

    return references


def _read_secret_metadata(kubectl, namespace, name):
    template = r'{{.type}}{{"\n"}}{{range $key, $_ := .data}}{{$key}}{{"\n"}}{{end}}'
    result = subprocess.run(
        [kubectl, "get", "secret", name, "--namespace", namespace,
         f"--output=go-template={template}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None, result.stderr
    lines = result.stdout.splitlines()
    return (lines[0] if lines else "Opaque", set(lines[1:])), ""


def validate_references(references, kubectl="kubectl"):
    failures = []
    for (namespace, name), reference in sorted(references.items()):
        metadata, error = _read_secret_metadata(kubectl, namespace, name)
        if metadata is None:
            if not reference["required"] and "not found" in error.lower():
                continue
            failures.append(f"Secret {namespace}/{name} is missing or unreadable")
            continue

        secret_type, keys = metadata
        missing_keys = sorted(
            key for key, required in reference["keys"].items()
            if required and key not in keys
        )
        if missing_keys:
            failures.append(
                f"Secret {namespace}/{name} is missing required key(s): "
                + ", ".join(missing_keys)
            )
        expected_types = reference["types"]
        if expected_types and secret_type not in expected_types:
            failures.append(
                f"Secret {namespace}/{name} has type {secret_type!r}; expected "
                + " or ".join(sorted(expected_types))
            )
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", required=True, type=Path)
    parser.add_argument("--release", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--values", action="append", default=[])
    parser.add_argument("--helm", default="helm")
    parser.add_argument("--kubectl", default="kubectl")
    args = parser.parse_args(argv)

    command = [args.helm, "template", args.release, str(args.chart),
               "--namespace", args.namespace]
    for values_file in args.values:
        command.extend(("--values", values_file))
    try:
        rendered = subprocess.run(
            command, capture_output=True, text=True, check=False
        )
        if rendered.returncode != 0:
            print("Secret preflight could not render the chart", file=sys.stderr)
            return 1
        documents = list(yaml.safe_load_all(rendered.stdout))
        references = collect_secret_references(documents, args.namespace)
        failures = validate_references(references, args.kubectl)
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        print("Secret preflight could not inspect chart Secret references", file=sys.stderr)
        return 1

    if failures:
        for failure in failures:
            print(f"Secret preflight: {failure}", file=sys.stderr)
        return 1
    print(f"Secret preflight passed for {args.release} in {args.namespace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())