import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HELM_DIR = Path(__file__).resolve().parent
USERNAME = "registry-test-user"
TOKEN = 'registry-test-sentinel-"\\-only'


class RegistryValuesTests(unittest.TestCase):
    def render(self, chart, values):
        result = subprocess.run(
            ["helm", "template", "local", str(HELM_DIR / chart), "--values", "-"],
            input=yaml.safe_dump(values), text=True, capture_output=True, check=False,
        )
        return result

    def documents(self, chart, values):
        result = self.render(chart, values)
        self.assertEqual(result.returncode, 0, "Sentinel-only Helm render failed")
        return [document for document in yaml.safe_load_all(result.stdout) if document]

    def test_model_endpoint_defaults_and_overrides(self):
        default = "http://inference-server.inference.svc.cluster.local:8000/v1"
        cases = [
            ({}, default, default),
            ({"supervisor": {"config": {"judge-url": "https://judge.example.com/v1"}}},
             "https://judge.example.com/v1", "https://judge.example.com/v1"),
            ({"worker": {"coderUrl": "https://coder.example.com/v1"}},
             default, "https://coder.example.com/v1"),
        ]
        for values, judge, coder in cases:
            with self.subTest(values=values):
                documents = self.documents("orchestrator", values)
                config = next(document for document in documents if document["kind"] == "ConfigMap")
                job = next(document for document in documents if document["kind"] == "Job")
                env = {entry["name"]: entry for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]}
                self.assertEqual(config["data"]["judge-url"], judge)
                self.assertEqual(env["CODER_URL"]["value"], coder)
                self.assertEqual(env["JUDGE_URL"]["valueFrom"]["configMapKeyRef"]["key"], "judge-url")

    def test_registry_auth_and_pod_references(self):
        for chart in ("orchestrator", "inference"):
            with self.subTest(chart=chart):
                documents = self.documents(chart, {
                    "registry": {"create": True, "username": USERNAME, "token": TOKEN},
                })
                secrets = [document for document in documents
                           if document["kind"] == "Secret"
                           and document.get("type") == "kubernetes.io/dockerconfigjson"]
                self.assertEqual(len(secrets), 1)
                secret = secrets[0]
                self.assertEqual(secret["metadata"]["name"], "ghcr-secret")
                self.assertEqual(secret["metadata"]["namespace"], chart)
                config = json.loads(base64.b64decode(secret["data"][".dockerconfigjson"]))
                self.assertEqual(set(config["auths"]), {"ghcr.io"})
                auth = config["auths"]["ghcr.io"]
                self.assertEqual(auth["username"], USERNAME)
                self.assertEqual(auth["password"], TOKEN)
                self.assertEqual(base64.b64decode(auth["auth"]).decode(), f"{USERNAME}:{TOKEN}")
                workloads = [document for document in documents
                             if document["kind"] in ("Deployment", "Job")]
                self.assertEqual(len(workloads), 3 if chart == "orchestrator" else 2)
                for workload in workloads:
                    self.assertEqual(workload["metadata"]["namespace"], chart)
                    self.assertIn({"name": "ghcr-secret"},
                                  workload["spec"]["template"]["spec"]["imagePullSecrets"])

    def test_default_and_external_secret_modes(self):
        for chart in ("orchestrator", "inference"):
            for custom in (False, True):
                with self.subTest(chart=chart, custom=custom):
                    values = {}
                    name = "external-pull-secret" if custom else "ghcr-secret"
                    if custom:
                        target = values.setdefault("inference", {}) if chart == "inference" else values
                        target["imagePullSecrets"] = [{"name": name}]
                    documents = self.documents(chart, values)
                    self.assertFalse(any(document.get("type") == "kubernetes.io/dockerconfigjson"
                                         for document in documents))
                    self.assertFalse(any(document["kind"] == "Secret" and
                                         document["metadata"]["name"] == "orchestrator-gateway-auth"
                                         for document in documents))
                    for document in documents:
                        if document["kind"] in ("Deployment", "Job"):
                            self.assertIn({"name": name},
                                          document["spec"]["template"]["spec"]["imagePullSecrets"])

    def test_invalid_registry_credentials_fail(self):
        invalid = [
            {"create": True},
            {"create": True, "username": USERNAME},
            {"create": True, "token": TOKEN},
            {"create": True, "username": " ", "token": TOKEN},
            {"create": True, "username": USERNAME, "token": "<replace-me>"},
            {"create": True, "username": "<your-github-username>", "token": TOKEN},
            {"create": False, "username": USERNAME, "token": TOKEN},
            {"create": True, "username": USERNAME, "token": TOKEN, "unknown": True},
        ]
        for chart in ("orchestrator", "inference"):
            for index, registry in enumerate(invalid):
                with self.subTest(chart=chart, case=index):
                    result = self.render(chart, {"registry": registry})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("registry", result.stderr)
                    self.assertNotIn("kind: Secret", result.stdout)

    def test_creation_reference_conflicts_fail(self):
        for chart in ("orchestrator", "inference"):
            values = {"registry": {"create": True, "username": USERNAME, "token": TOKEN}}
            target = values.setdefault("inference", {}) if chart == "inference" else values
            target["imagePullSecrets"] = [{"name": "external-pull-secret"}]
            result = self.render(chart, values)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires", result.stderr)

    def test_gateway_file_token_remains_separate(self):
        gateway_token = "gateway-only-sentinel-independent-32-characters"
        documents = self.documents("orchestrator", {
            "gateway": {"auth": {"existingSecret": "", "token": gateway_token}},
            "supervisor": {"config": {"openai-api-key": "model-key-sentinel"}},
        })
        secrets = {document["metadata"]["name"]: document for document in documents
                   if document["kind"] == "Secret"}
        token = secrets["orchestrator-gateway-auth"]["data"]["gateway-auth-token"]
        self.assertEqual(base64.b64decode(token).decode(), gateway_token)
        model_key = secrets["orchestrator-secrets"]["data"]["openai-api-key"]
        self.assertEqual(base64.b64decode(model_key).decode(), "model-key-sentinel")
        result = self.render("orchestrator", {
            "gateway": {"auth": {"existingSecret": "", "token": "<replace-with-independent-token-32-characters>"}},
        })
        self.assertNotEqual(result.returncode, 0)

    def test_external_proxy_example(self):
        example = HELM_DIR / "orchestrator" / "example.values.yaml"
        contents = example.read_text()
        self.assertNotIn("utopiklab.com", contents)
        values = yaml.safe_load(contents)
        self.assertEqual(values["supervisor"]["config"]["acceptor-url"],
                         "https://gateway.example.com")
        documents = self.documents("orchestrator", values)
        ingress = next(document for document in documents if document["kind"] == "Ingress")
        self.assertEqual(ingress["spec"]["rules"][0]["host"], "orchestrator.local")
        self.assertNotIn("tls", ingress["spec"])
        self.assertEqual(ingress["metadata"]["annotations"][
            "traefik.ingress.kubernetes.io/router.entrypoints"], "web")

    def test_deploy_values_path_and_failure_propagation(self):
        with tempfile.TemporaryDirectory(prefix="helm registry test-") as directory:
            root = Path(directory)
            script = root / "deploy.sh"
            shutil.copyfile(HELM_DIR / "deploy.sh", script)
            default_private = root / "local.secrets-values.yaml"
            custom_private = root / "private values.yaml"
            chart_dir = root / "charts"
            (chart_dir / "orchestrator").mkdir(parents=True)
            default_overlay = chart_dir / "orchestrator" / "local.values.yaml"
            custom_overlay = root / "custom proxy values.yaml"
            log = root / "calls.jsonl"
            for command in ("helm", "kubectl"):
                executable = root / command
                executable.write_text(
                    f"#!{sys.executable}\n"
                    "import json, os, sys\n"
                    "with open(os.environ['TEST_CALLS'], 'a') as stream:\n"
                    "    stream.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + '\\n')\n"
                    "if sys.argv[1:2] == ['upgrade']:\n"
                    "    sys.exit(int(os.environ.get('TEST_UPGRADE_EXIT', '0')))\n"
                )
                executable.chmod(0o700)
            environment = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                               HELM_DIR=str(chart_dir), TEST_CALLS=str(log))
            environment.pop("ORCHESTRATOR_VALUES_FILE", None)
            for overlay_mode in ("absent", "default", "custom"):
                default_overlay.unlink(missing_ok=True)
                custom_overlay.unlink(missing_ok=True)
                environment.pop("ORCHESTRATOR_VALUES_FILE", None)
                overlay = custom_overlay if overlay_mode == "custom" else default_overlay
                if overlay_mode != "absent":
                    overlay.write_text("gateway:\n  ingress:\n    tlsTermination: external\n")
                if overlay_mode == "custom":
                    environment["ORCHESTRATOR_VALUES_FILE"] = str(overlay)
                for private_mode in ("absent", "default", "custom"):
                    default_private.unlink(missing_ok=True)
                    custom_private.unlink(missing_ok=True)
                    environment.pop("SECRETS_FILE", None)
                    values = custom_private if private_mode == "custom" else default_private
                    if private_mode != "absent":
                        values.write_text("registry:\n  create: false\n")
                    if private_mode == "custom":
                        environment["SECRETS_FILE"] = str(values)
                    for failure in (False, True):
                        with self.subTest(overlay=overlay_mode, private=private_mode, failure=failure):
                            log.unlink(missing_ok=True)
                            environment["TEST_UPGRADE_EXIT"] = "23" if failure else "0"
                            result = subprocess.run(["bash", str(script)],
                                                    env=environment, capture_output=True,
                                                    text=True, check=False)
                            self.assertEqual(result.returncode, 23 if failure else 0)
                            calls = [json.loads(line) for line in log.read_text().splitlines()]
                            upgrades = [call for call in calls if call[:2] == ["helm", "upgrade"]]
                            self.assertEqual(len(upgrades), 1 if failure else 2)
                            for call in upgrades:
                                expected = []
                                if "orchestrator" in call and overlay_mode != "absent":
                                    expected.append(str(overlay))
                                if private_mode != "absent":
                                    expected.append(str(values))
                                actual = [call[index + 1] for index, argument in enumerate(call)
                                          if argument == "--values"]
                                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()