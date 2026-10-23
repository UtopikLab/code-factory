import importlib.util
import unittest
from unittest.mock import patch

SCRIPT_PATH = __import__("pathlib").Path(__file__).with_name("validate_secret_refs.py")
SPEC = importlib.util.spec_from_file_location("validate_secret_refs", SCRIPT_PATH)
validate_secret_refs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validate_secret_refs)


class CollectSecretReferencesTests(unittest.TestCase):
    def test_collects_pod_keys_pull_secret_and_ingress_tls(self):
        documents = [
            {
                "kind": "Deployment",
                "metadata": {"namespace": "orchestrator"},
                "spec": {"template": {"spec": {
                    "imagePullSecrets": [{"name": "registry"}],
                    "containers": [{"env": [{"valueFrom": {"secretKeyRef": {
                        "name": "service-token", "key": "token"
                    }}}]}],
                    "volumes": [{"secret": {"secretName": "app-key", "items": [
                        {"key": "private-key.pem"}
                    ]}}],
                }}},
            },
            {
                "kind": "Ingress",
                "metadata": {"namespace": "orchestrator"},
                "spec": {"tls": [{"secretName": "gateway-tls"}]},
            },
        ]

        references = validate_secret_refs.collect_secret_references(
            documents, "default"
        )

        self.assertEqual(
            references[("orchestrator", "registry")]["keys"],
            {".dockerconfigjson": True},
        )
        self.assertEqual(
            references[("orchestrator", "service-token")]["keys"],
            {"token": True},
        )
        self.assertEqual(
            references[("orchestrator", "app-key")]["keys"],
            {"private-key.pem": True},
        )
        self.assertEqual(
            references[("orchestrator", "gateway-tls")]["types"],
            {"kubernetes.io/tls"},
        )

    def test_optional_secret_key_can_be_absent(self):
        references = validate_secret_refs.collect_secret_references(
            [{"metadata": {"namespace": "orchestrator"}, "env": [{
                "secretKeyRef": {"name": "optional-api-key", "key": "api-key",
                                 "optional": True}
            }]}],
            "default",
        )

        with patch.object(
            validate_secret_refs,
            "_read_secret_metadata",
            return_value=(None, "Error from server (NotFound): secrets not found"),
        ):
            self.assertEqual(validate_secret_refs.validate_references(references), [])


class ValidateSecretReferencesTests(unittest.TestCase):
    @patch.object(validate_secret_refs.subprocess, "run")
    def test_validates_keys_and_types_without_emitting_secret_values(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = (
            "kubernetes.io/dockerconfigjson\n.dockerconfigjson\n"
        )
        run.return_value.stderr = ""
        references = {
            ("inference", "pull-secret"): {
                "required": True,
                "keys": {".dockerconfigjson": True},
                "types": {"kubernetes.io/dockerconfigjson"},
            }
        }

        failures = validate_secret_refs.validate_references(references)

        self.assertEqual(failures, [])
        self.assertIn("--output=go-template=", run.call_args.args[0][-1])
        self.assertNotIn("password", run.call_args.args[0][-1].lower())

    @patch.object(validate_secret_refs.subprocess, "run")
    def test_reports_missing_key_and_wrong_type(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "Opaque\nother-key\n"
        run.return_value.stderr = ""
        references = {
            ("orchestrator", "gateway-tls"): {
                "required": True,
                "keys": {"tls.crt": True, "tls.key": True},
                "types": {"kubernetes.io/tls"},
            }
        }

        failures = validate_secret_refs.validate_references(references)

        self.assertEqual(len(failures), 2)
        self.assertIn("tls.crt, tls.key", failures[0])
        self.assertIn("expected kubernetes.io/tls", failures[1])


if __name__ == "__main__":
    unittest.main()