import base64
import json
import os
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

    def test_deploy_values_path_and_failure_propagation(self):
        with tempfile.TemporaryDirectory(prefix="helm-registry-test-") as directory:
            root = Path(directory)
            values = root / "private values.yaml"
            values.write_text("registry:\n  create: false\n")
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
                               SECRETS_FILE=str(values), HELM_DIR=str(HELM_DIR), TEST_CALLS=str(log))
            for failure in (False, True):
                with self.subTest(failure=failure):
                    log.unlink(missing_ok=True)
                    environment["TEST_UPGRADE_EXIT"] = "23" if failure else "0"
                    result = subprocess.run(["bash", str(HELM_DIR / "deploy.sh")],
                                            env=environment, capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 23 if failure else 0)
                    calls = [json.loads(line) for line in log.read_text().splitlines()]
                    upgrades = [call for call in calls if call[:2] == ["helm", "upgrade"]]
                    self.assertEqual(len(upgrades), 1 if failure else 2)
                    for call in upgrades:
                        self.assertEqual(call[call.index("--values") + 1], str(values))


if __name__ == "__main__":
    unittest.main()