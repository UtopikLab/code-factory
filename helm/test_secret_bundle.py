import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_SPEC = importlib.util.spec_from_file_location(
    "render_secret_bundle", SCRIPT_DIR / "render-secret-bundle.py"
)
render_secret_bundle = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(render_secret_bundle)


class DotenvParserTests(unittest.TestCase):
    def test_unquoted_and_quoted_values_are_parsed_as_data(self):
        values = render_secret_bundle.parse_dotenv(
            'PLAIN=ordinary value\nQUOTED="line one\\nline two"\n'
            'LITERAL=$(touch /tmp/not-created)\n'
        )

        self.assertEqual(values["PLAIN"], "ordinary value")
        self.assertEqual(values["QUOTED"], "line one\nline two")
        self.assertEqual(values["LITERAL"], "$(touch /tmp/not-created)")

    def test_malformed_quotes_escapes_and_duplicates_are_rejected(self):
        invalid_inputs = (
            'VALUE="unterminated',
            'VALUE=un"escaped',
            'VALUE="unsupported\\t"',
            "VALUE=one\nVALUE=two",
        )

        for dotenv in invalid_inputs:
            with self.subTest(dotenv=dotenv), self.assertRaises(ValueError):
                render_secret_bundle.parse_dotenv(dotenv)


class SecretBundleRenderTests(unittest.TestCase):
    def setUp(self):
        self.bundle = {
            key: "test-value" for key in render_secret_bundle.REQUIRED_KEYS
        }
        self.bundle.update({key: "" for key in render_secret_bundle.OPTIONAL_KEYS})
        self.inference_values = render_secret_bundle.load_values(
            [SCRIPT_DIR / "inference" / "values.yaml"]
        )
        self.orchestrator_values = render_secret_bundle.load_values(
            [
                SCRIPT_DIR / "orchestrator" / "values.yaml",
                SCRIPT_DIR / "orchestrator" / "local.values.yaml",
            ]
        )

    def test_chart_defaults_and_local_overlay_render_without_optional_secrets(self):
        documents = render_secret_bundle.render(
            self.bundle, self.inference_values, self.orchestrator_values
        )

        self.assertEqual(len(documents), 8)

    def test_empty_required_secret_ref_reports_values_path(self):
        self.orchestrator_values["secretRefs"]["githubAppKey"]["name"] = ""

        with self.assertRaisesRegex(
            ValueError, "secretRefs\\.githubAppKey\\.name is empty in chart values"
        ):
            render_secret_bundle.render(
                self.bundle, self.inference_values, self.orchestrator_values
            )

    def test_populated_optional_value_reports_empty_optional_ref_path(self):
        self.bundle["OPENAI_API_KEY"] = "test-optional-value"

        with self.assertRaisesRegex(
            ValueError, "secretRefs\\.openaiApiKey\\.name is empty in chart values"
        ):
            render_secret_bundle.render(
                self.bundle, self.inference_values, self.orchestrator_values
            )

    def test_empty_inference_pull_secret_reports_values_path(self):
        self.inference_values["inference"]["imagePullSecrets"][0]["name"] = ""

        with self.assertRaisesRegex(
            ValueError,
            "inference\\.imagePullSecrets\\[0\\]\\.name is empty in chart values",
        ):
            render_secret_bundle.render(
                self.bundle, self.inference_values, self.orchestrator_values
            )


class ApplySecretBundleTests(unittest.TestCase):
    def test_kubectl_failure_returns_nonzero_without_success_message(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            helm_directory = root / "helm"
            bin_directory = root / "bin"
            helm_directory.mkdir()
            bin_directory.mkdir()

            script = helm_directory / "apply-secret-bundle.sh"
            shutil.copyfile(SCRIPT_DIR / "apply-secret-bundle.sh", script)
            script.chmod(0o700)
            (root / ".gitignore").write_text(
                "helm/local.secret-bundle.env\n", encoding="utf-8"
            )
            (helm_directory / "local.secret-bundle.env").write_text(
                "TEST_ONLY=dummy\n", encoding="utf-8"
            )
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)

            renderer = bin_directory / "python3"
            renderer.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'kind: Namespace'\n", encoding="utf-8"
            )
            renderer.chmod(0o700)
            kubectl_marker = root / "kubectl-called"
            kubectl = bin_directory / "kubectl"
            kubectl.write_text(
                "#!/bin/sh\nprintf called > \"$KUBECTL_MARKER\"\nexit 23\n",
                encoding="utf-8",
            )
            kubectl.chmod(0o700)

            environment = os.environ.copy()
            environment["PATH"] = f"{bin_directory}{os.pathsep}{environment['PATH']}"
            environment["KUBECTL_MARKER"] = str(kubectl_marker)
            result = subprocess.run(
                [str(script)],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertNotIn("External Secrets and namespaces applied", result.stdout)
            self.assertIn("local Secret input failed validation or apply", result.stderr)
            self.assertTrue(kubectl_marker.is_file())


if __name__ == "__main__":
    unittest.main()