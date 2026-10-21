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
AUTH_CREDENTIALS = {
    "internal": {
        "gatewayClientToken": "gateway-test-sentinel-at-least-32-characters",
        "supervisorServiceToken": "supervisor-test-sentinel",
    },
    "github": {"pullRequestPat": "pr-test-sentinel"},
}


class RegistryValuesTests(unittest.TestCase):
    def test_inference_native_router_and_cache(self):
        values = yaml.safe_load((HELM_DIR / "inference/values.yaml").read_text())["inference"]
        documents = self.documents("inference", {})
        deployments = [document for document in documents if document["kind"] == "Deployment"]
        self.assertEqual(len(deployments), 2)
        caches = set()
        for deployment, config in zip(deployments, values["configMaps"]):
            pod = deployment["spec"]["template"]["spec"]
            server = pod["containers"][0]
            self.assertNotIn("initContainers", pod)
            self.assertEqual(deployment["spec"]["replicas"], 1)
            self.assertEqual(server["args"][:2], ["--models-max", "1"])
            for flag in ("--models-dir", "--model", "--hf-repo", "--hf-file", "--alias",
                         "--models-preset", "--no-models-autoload", "--rope-freq-base", "--rope-freq-scale"):
                self.assertNotIn(flag, server["args"])
            service = next(document for document in documents if document["kind"] == "Service"
                           and document["metadata"]["name"] == config["name"])
            self.assertEqual(service["spec"]["selector"], deployment["spec"]["selector"]["matchLabels"])
            environment = {entry["name"]: entry["value"] for entry in server["env"]}
            mount = server["volumeMounts"][1]
            self.assertEqual(mount["mountPath"], config["model"]["model-dir"])
            self.assertFalse(mount.get("readOnly", False))
            self.assertEqual(environment["LLAMA_CACHE"], f'{mount["mountPath"]}/llama.cpp/{config["name"]}')
            caches.add(environment["LLAMA_CACHE"])
            volume = next(volume for volume in pod["volumes"] if volume["name"] == mount["name"])
            self.assertEqual(volume["hostPath"]["path"], "/mnt/k8s-local-nvme/inference-models")
            self.assertEqual(volume["hostPath"]["type"], "DirectoryOrCreate")
            self.assertEqual(server["args"][server["args"].index("--ctx-size") + 1], "32768")
            for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
                self.assertEqual(server[probe]["httpGet"]["path"], "/health")
        self.assertEqual(len(caches), 2)

    def test_inference_download_overrides_and_two_server_rollout(self):
        documents = self.documents("inference", {"inference": {
            "gpuCount": 1,
            "pvc": {"name": "custom-models"},
            "configMaps": [{"name": "custom-server", "model": {
                "model-dir": "/weights", "ctx-size": "8192",
            }}],
        }})
        deployment = next(document for document in documents if document["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        pod = deployment["spec"]["template"]["spec"]
        server = pod["containers"][0]
        self.assertNotIn("--alias", server["args"])
        self.assertEqual(server["args"][server["args"].index("--ctx-size") + 1], "8192")
        self.assertEqual(server["env"], [{"name": "LLAMA_CACHE", "value": "/weights/llama.cpp/custom-server"}])
        self.assertEqual(server["volumeMounts"][1]["mountPath"], "/weights")
        self.assertEqual(pod["volumes"][1]["hostPath"]["path"], "/mnt/k8s-local-nvme/custom-models")
        defaults = self.documents("inference", {"inference": {"gpuCount": 1}})
        deployments = [document for document in defaults if document["kind"] == "Deployment"]
        self.assertEqual(sum(deployment["spec"]["replicas"] for deployment in deployments), 2)

    def test_fixed_model_sources_fail_schema(self):
        for source in ({}, {"repo": "owner/model"}, {"filename": "model.gguf"},
                       {"localPath": "/models/checkpoints/model.gguf"}):
            with self.subTest(source=source):
                result = self.render("inference", {"inference": {"modelSource": source}})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("modelSource", result.stderr)

    def test_inference_duplicate_cache_writers_fail_schema(self):
        result = self.render("inference", {"inference": {"gpuCount": 2}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gpuCount", result.stderr)

    def render(self, chart, values, provide_auth=True):
        values = json.loads(json.dumps(values))
        if chart == "orchestrator" and provide_auth:
            credentials = values.setdefault("credentials", {})
            for group, defaults in AUTH_CREDENTIALS.items():
                target = credentials.setdefault(group, {})
                for key, value in defaults.items():
                    target.setdefault(key, value)
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
        default = "http://llama-server-args-gpu1.inference.svc.cluster.local:8000/v1"
        coder_default = "http://llama-server-args-gpu0.inference.svc.cluster.local:8000/v1"
        cases = [
            ({}, default, coder_default),
            ({"supervisor": {"config": {"judge-url": "https://judge.example.com/v1"}}},
             "https://judge.example.com/v1", coder_default),
            ({"worker": {"coderUrl": "https://coder.example.com/v1"}},
             default, "https://coder.example.com/v1"),
        ]
        for values, judge, coder in cases:
            with self.subTest(values=values):
                documents = self.documents("orchestrator", values)
                config = next(document for document in documents if document["kind"] == "ConfigMap"
                              and document["metadata"]["name"] == "orchestrator-config")
                catalog = next(document for document in documents if document["kind"] == "ConfigMap"
                               and document["metadata"]["name"].startswith("orchestrator-agent-catalog-"))
                runtime = json.loads(catalog["data"]["runtime-config.json"])
                job = next(document for document in documents if document["kind"] == "Job")
                env = {entry["name"]: entry for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]}
                self.assertEqual(config["data"]["judge-url"], judge)
                self.assertEqual(env["CODER_URL"]["value"], coder)
                self.assertEqual(env["CODER_NATIVE_DOWNLOAD"]["value"], str(coder == coder_default).lower())
                self.assertEqual(runtime["judge_url"], judge)
                self.assertEqual(runtime["coder_url"], coder)
                self.assertEqual(runtime["judge_native_download"], judge == default)
                self.assertNotIn("JUDGE_NATIVE_DOWNLOAD", env)
                self.assertNotIn("JUDGE_URL", env)
                supervisor = next(document for document in documents if document["kind"] == "Deployment"
                                  and document["metadata"]["name"] == "orchestrator-supervisor")
                supervisor_env = {entry["name"]: entry for entry in supervisor["spec"]["template"]["spec"]["containers"][0]["env"]}
                self.assertEqual(supervisor_env["JUDGE_URL"]["valueFrom"]["configMapKeyRef"], {
                    "name": "orchestrator-config", "key": "judge-url",
                })

    def test_native_download_switches_can_be_overridden(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                documents = self.documents("orchestrator", {"worker": {
                    "coderUrl": "https://custom-router/v1", "coderNativeDownload": enabled,
                    "judgeNativeDownload": enabled,
                }})
                job = next(document for document in documents if document["kind"] == "Job")
                env = {entry["name"]: entry for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]}
                catalog = next(document for document in documents if document["kind"] == "ConfigMap"
                               and document["metadata"]["name"].startswith("orchestrator-agent-catalog-"))
                runtime = json.loads(catalog["data"]["runtime-config.json"])
                self.assertEqual(env["CODER_NATIVE_DOWNLOAD"]["value"], str(enabled).lower())
                self.assertEqual(runtime["coder_native_download"], enabled)
                self.assertEqual(runtime["judge_native_download"], enabled)
                self.assertNotIn("JUDGE_NATIVE_DOWNLOAD", env)

    def test_requester_model_defaults_use_native_hf_identifier(self):
        model = "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M"
        documents = self.documents("orchestrator", {})
        config = next(document for document in documents if document["kind"] == "ConfigMap"
                  and document["metadata"]["name"] == "orchestrator-config")
        catalog = next(document for document in documents if document["kind"] == "ConfigMap"
                   and document["metadata"]["name"].startswith("orchestrator-agent-catalog-"))
        runtime = json.loads(catalog["data"]["runtime-config.json"])
        job = next(document for document in documents if document["kind"] == "Job")
        env = {entry["name"]: entry for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["CODER_MODEL"]["value"], model)
        self.assertEqual(env["CODER_NATIVE_DOWNLOAD"]["value"], "true")
        self.assertTrue(runtime["judge_native_download"])
        self.assertEqual(runtime["judge_model"], model)
        self.assertEqual(runtime["coder_model"], model)
        self.assertEqual(config["data"]["judge-model"], model)
        self.assertNotIn("JUDGE_MODEL", env)

    def test_model_overrides_are_preserved(self):
        documents = self.documents("orchestrator", {
            "supervisor": {"config": {"judge-model": "custom-judge"}},
            "worker": {"worker-model": "custom-coder"},
        })
        config = next(document for document in documents if document["kind"] == "ConfigMap"
                  and document["metadata"]["name"] == "orchestrator-config")
        job = next(document for document in documents if document["kind"] == "Job")
        env = {entry["name"]: entry for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["CODER_MODEL"]["value"], "custom-coder")
        self.assertEqual(config["data"]["judge-model"], "custom-judge")

    def test_registry_auth_and_pod_references(self):
        for chart in ("orchestrator", "inference"):
            with self.subTest(chart=chart):
                documents = self.documents(chart, {
                    "registry": {"create": True, "username": USERNAME},
                    "credentials": {"github": {"ghcrReadPackagesPat": TOKEN}},
                })
                secrets = [document for document in documents
                           if document["kind"] == "Secret"
                           and document.get("type") == "kubernetes.io/dockerconfigjson"]
                self.assertEqual(len(secrets), 1)
                secret = secrets[0]
                self.assertEqual(secret["metadata"]["name"], "ghcr-read-packages-pull-secret")
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
                    self.assertIn({"name": "ghcr-read-packages-pull-secret"},
                                  workload["spec"]["template"]["spec"]["imagePullSecrets"])

    def test_grouped_ghcr_pat_is_shared_with_inference_chart(self):
        documents = self.documents("inference", {
            "registry": {"create": True, "username": USERNAME},
            "credentials": {
                "internal": {
                    "gatewayClientToken": "gateway-shared-dummy-token-32-characters",
                    "supervisorServiceToken": "supervisor-shared-dummy",
                },
                "github": {
                    "ghcrReadPackagesPat": "ghcr-shared-dummy",
                    "pullRequestPat": "pr-shared-dummy",
                },
                "models": {
                    "openaiApiKey": "openai-shared-dummy",
                    "coderApiKey": "coder-shared-dummy",
                },
            },
        })
        secret = next(document for document in documents
                      if document["kind"] == "Secret" and document["metadata"]["name"] == "ghcr-read-packages-pull-secret")
        config = json.loads(base64.b64decode(secret["data"][".dockerconfigjson"]))
        self.assertEqual(config["auths"]["ghcr.io"]["password"], "ghcr-shared-dummy")

    def test_grouped_credentials_route_to_expected_consumers(self):
        credentials = {
            "internal": {
                "gatewayClientToken": "gateway-grouped-sentinel-token-32-characters",
                "supervisorServiceToken": "supervisor-grouped-sentinel",
            },
            "github": {
                "ghcrReadPackagesPat": "ghcr-grouped-sentinel",
                "pullRequestPat": "pr-grouped-sentinel",
            },
            "models": {
                "openaiApiKey": "openai-grouped-sentinel",
                "coderApiKey": "coder-grouped-sentinel",
            },
        }
        documents = self.documents("orchestrator", {
            "credentials": credentials,
            "registry": {"create": True, "username": USERNAME},
        })
        secrets = {document["metadata"]["name"]: document for document in documents
                   if document["kind"] == "Secret"}
        self.assertEqual(set(secrets), {
            "orchestrator-openai-model-auth", "orchestrator-supervisor-service-auth",
            "orchestrator-github-pr-auth", "orchestrator-gateway-auth",
            "orchestrator-coder-model-auth", "ghcr-read-packages-pull-secret",
        })
        for name, key in (
            ("orchestrator-openai-model-auth", "openai-api-key"),
            ("orchestrator-supervisor-service-auth", "supervisor-auth-token"),
            ("orchestrator-github-pr-auth", "pr-github-token"),
            ("orchestrator-gateway-auth", "gateway-auth-token"),
            ("orchestrator-coder-model-auth", "coder-api-key"),
        ):
            self.assertEqual(set(secrets[name]["data"]), {key})
        self.assertEqual(base64.b64decode(secrets["orchestrator-openai-model-auth"]["data"]["openai-api-key"]).decode(),
                         "openai-grouped-sentinel")
        self.assertEqual(base64.b64decode(secrets["orchestrator-supervisor-service-auth"]["data"]["supervisor-auth-token"]).decode(),
                         "supervisor-grouped-sentinel")
        self.assertEqual(base64.b64decode(secrets["orchestrator-github-pr-auth"]["data"]["pr-github-token"]).decode(),
                         "pr-grouped-sentinel")
        self.assertEqual(base64.b64decode(secrets["orchestrator-coder-model-auth"]["data"]["coder-api-key"]).decode(),
                         "coder-grouped-sentinel")
        docker_config = json.loads(base64.b64decode(secrets["ghcr-read-packages-pull-secret"]["data"][".dockerconfigjson"]))
        self.assertEqual(docker_config["auths"]["ghcr.io"]["password"], "ghcr-grouped-sentinel")

        gateway = next(document for document in documents if document["kind"] == "Deployment"
                       and document["metadata"]["name"] == "orchestrator-gateway")
        gateway_env = {entry["name"]: entry for entry in gateway["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(gateway_env["CODEFACTORY_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"], {
            "name": "orchestrator-gateway-auth", "key": "gateway-auth-token",
        })
        self.assertEqual(base64.b64decode(secrets["orchestrator-gateway-auth"]["data"]["gateway-auth-token"]).decode(),
                         "gateway-grouped-sentinel-token-32-characters")
        self.assertEqual(gateway_env["SUPERVISOR_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"], {
            "name": "orchestrator-supervisor-service-auth", "key": "supervisor-auth-token",
        })

        supervisor = next(document for document in documents if document["kind"] == "Deployment"
                          and document["metadata"]["name"] == "orchestrator-supervisor")
        supervisor_env = {entry["name"]: entry for entry in supervisor["spec"]["template"]["spec"]["containers"][0]["env"]}
        for name, secret_name, key in (
            ("OPENAI_API_KEY", "orchestrator-openai-model-auth", "openai-api-key"),
            ("SUPERVISOR_AUTH_TOKEN", "orchestrator-supervisor-service-auth", "supervisor-auth-token"),
            ("PR_GITHUB_TOKEN", "orchestrator-github-pr-auth", "pr-github-token"),
        ):
            self.assertEqual(supervisor_env[name]["valueFrom"]["secretKeyRef"], {
                "name": secret_name, "key": key,
            })

        worker = next(document for document in documents if document["kind"] == "Job")
        worker_env = {entry["name"]: entry for entry in worker["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(worker_env["CODER_API_KEY"]["valueFrom"]["secretKeyRef"], {
            "name": "orchestrator-coder-model-auth", "key": "coder-api-key",
        })

    def test_examples_separate_credentials_from_behavior(self):
        credentials = yaml.safe_load((HELM_DIR / "example.secrets-values.yaml").read_text())
        self.assertEqual(set(credentials), {"credentials", "management"})
        self.assertEqual(set(credentials["credentials"]), {"internal", "github", "models"})
        self.assertEqual(set(credentials["management"]), {"admin", "repositories", "oidc"})
        self.assertNotIn("createSecret", str(credentials))
        self.assertNotIn("existingSecret", str(credentials))

        shared = yaml.safe_load((HELM_DIR / "example.values.yaml").read_text())
        self.assertEqual(shared["registry"], {"create": True, "username": "<your-github-username>"})
        orchestrator = yaml.safe_load((HELM_DIR / "orchestrator/example.values.yaml").read_text())
        for component in ("gateway", "supervisor", "worker"):
            self.assertNotIn("auth", orchestrator.get(component, {}))

    def test_deploy_value_file_order_renders_secrets_and_references(self):
        shared_values = yaml.safe_load((HELM_DIR / "example.values.yaml").read_text())
        shared_values["registry"]["username"] = USERNAME
        orchestrator_values = yaml.safe_load(
            (HELM_DIR / "orchestrator/example.values.yaml").read_text())
        secret_values = {"credentials": {
            "internal": {
                "gatewayClientToken": "gateway-layered-dummy-token-at-least-32-chars",
                "supervisorServiceToken": "supervisor-layered-dummy",
            },
            "github": {
                "ghcrReadPackagesPat": "ghcr-layered-dummy",
                "pullRequestPat": "pr-layered-dummy",
            },
            "models": {
                "openaiApiKey": "openai-layered-dummy",
                "coderApiKey": "coder-layered-dummy",
            },
        }}

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            paths = []
            for name, values in (("shared.yaml", shared_values),
                                 ("orchestrator.yaml", orchestrator_values),
                                 ("secrets.yaml", secret_values)):
                path = temporary_path / name
                path.write_text(yaml.safe_dump(values))
                paths.append(path)

            for chart in ("inference", "orchestrator"):
                with self.subTest(chart=chart):
                    ordered_paths = [paths[0]]
                    if chart == "orchestrator":
                        ordered_paths.append(paths[1])
                    ordered_paths.append(paths[2])
                    documents = self.documents_from_value_files(chart, ordered_paths)
                    registry_secret = next(document for document in documents
                                           if document["kind"] == "Secret"
                                           and document["metadata"]["name"] == "ghcr-read-packages-pull-secret")
                    self.assertEqual(registry_secret["metadata"]["namespace"], chart)
                    docker_config = json.loads(base64.b64decode(
                        registry_secret["data"][".dockerconfigjson"]))
                    self.assertEqual(docker_config["auths"]["ghcr.io"]["password"],
                                     "ghcr-layered-dummy")
                    workloads = [document for document in documents
                                 if document["kind"] in ("Deployment", "Job")]
                    for workload in workloads:
                        self.assertIn({"name": "ghcr-read-packages-pull-secret"},
                                      workload["spec"]["template"]["spec"]["imagePullSecrets"])

                    if chart == "inference":
                        continue
                    secrets = {document["metadata"]["name"]: document for document in documents
                               if document["kind"] == "Secret"}
                    self.assertEqual(set(secrets), {
                        "orchestrator-openai-model-auth", "orchestrator-supervisor-service-auth",
                        "orchestrator-github-pr-auth", "orchestrator-gateway-auth",
                        "orchestrator-coder-model-auth",
                        "ghcr-read-packages-pull-secret",
                    })
                    gateway = next(document for document in documents if document["kind"] == "Deployment"
                                   and document["metadata"]["name"] == "orchestrator-gateway")
                    gateway_env = {entry["name"]: entry for entry in
                                   gateway["spec"]["template"]["spec"]["containers"][0]["env"]}
                    self.assertEqual(gateway_env["CODEFACTORY_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"], {
                        "name": "orchestrator-gateway-auth", "key": "gateway-auth-token",
                    })
                    supervisor = next(document for document in documents if document["kind"] == "Deployment"
                                      and document["metadata"]["name"] == "orchestrator-supervisor")
                    supervisor_env = {entry["name"]: entry for entry in
                                      supervisor["spec"]["template"]["spec"]["containers"][0]["env"]}
                    self.assertEqual(supervisor_env["SUPERVISOR_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"], {
                        "name": "orchestrator-supervisor-service-auth", "key": "supervisor-auth-token",
                    })
                    worker = next(document for document in documents if document["kind"] == "Job")
                    worker_env = {entry["name"]: entry for entry in
                                  worker["spec"]["template"]["spec"]["containers"][0]["env"]}
                    self.assertEqual(worker_env["CODER_API_KEY"]["valueFrom"]["secretKeyRef"], {
                        "name": "orchestrator-coder-model-auth", "key": "coder-api-key",
                    })

    def documents_from_value_files(self, chart, value_files):
        command = ["helm", "template", "local", str(HELM_DIR / chart)]
        if chart == "orchestrator":
            command.extend(("--values", "-"))
        for path in value_files:
            command.extend(("--values", str(path)))
        result = subprocess.run(command, input=yaml.safe_dump({"credentials": AUTH_CREDENTIALS}),
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, "Layered dummy Helm render failed")
        return [document for document in yaml.safe_load_all(result.stdout) if document]

    def test_legacy_credential_paths_are_rejected(self):
        legacy_values = (
            {"registry": {"token": "old-ghcr-token"}},
            {"gateway": {"auth": {"token": "old-gateway-token-32-characters-long"}}},
            {"supervisor": {"auth": {"supervisor-auth-token": "old-service-token"}}},
            {"supervisor": {"auth": {"pr-github-token": "old-pr-token"}}},
            {"supervisor": {"config": {"openai-api-key": "old-model-key"}}},
        )
        for values in legacy_values:
            with self.subTest(values=values):
                result = self.render("orchestrator", values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("values don't meet the specifications", result.stderr)

    def test_removed_auth_configuration_is_rejected(self):
        for component, fields in (
            ("gateway", ("existingSecret",)),
            ("supervisor", ("existingSecret", "pullRequestSecret", "createSecret")),
            ("worker", ("existingSecret", "createSecret")),
        ):
            for field in fields:
                for value in ((None, False, True) if field == "createSecret" else ("external-auth", "")):
                    with self.subTest(component=component, field=field, value=value):
                        result = self.render("orchestrator", {component: {"auth": {field: value}}})
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("auth", result.stderr)

    def test_required_auth_credentials_fail_with_value_paths(self):
        result = self.render("orchestrator", {}, provide_auth=False)
        self.assertNotEqual(result.returncode, 0)
        for group, defaults in AUTH_CREDENTIALS.items():
            for key in defaults:
                for invalid in ("", " ", "<replace-me>"):
                    with self.subTest(group=group, key=key, invalid=invalid):
                        credentials = json.loads(json.dumps(AUTH_CREDENTIALS))
                        credentials[group][key] = invalid
                        result = self.render("orchestrator", {"credentials": credentials}, provide_auth=False)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(key, result.stderr)
                        self.assertNotIn("kind: Secret", result.stdout)

    def test_removed_api_absent_from_values_schema_and_docs(self):
        paths = [HELM_DIR / "orchestrator/values.schema.json",
                 HELM_DIR.parent / "docs/operations/credential-separation.md"]
        paths.extend(path for path in HELM_DIR.glob("**/*values.yaml")
                     if "charts" not in path.parts and path.name != "local.secrets-values.yaml")
        for path in paths:
            contents = path.read_text()
            for field in ("existingSecret", "pullRequestSecret", "createSecret", "repositoriesSecret"):
                with self.subTest(path=path.relative_to(HELM_DIR.parent), field=field):
                    self.assertNotIn(field, contents)

    def test_optional_model_credentials_reject_invalid_values(self):
        for key in ("openaiApiKey", "coderApiKey"):
            for invalid in (" ", "<replace-me>"):
                with self.subTest(key=key, invalid=invalid):
                    result = self.render("orchestrator", {"credentials": {"models": {key: invalid}}})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(key, result.stderr)

    def test_auth_resources_exist_with_optional_model_keys_empty(self):
        documents = self.documents("orchestrator", {})
        secrets = {document["metadata"]["name"]: document for document in documents
                   if document["kind"] == "Secret"}
        self.assertEqual(set(secrets), {
            "orchestrator-gateway-auth", "orchestrator-supervisor-service-auth",
            "orchestrator-github-pr-auth", "orchestrator-coder-model-auth",
            "orchestrator-openai-model-auth",
        })
        for name, key in (("orchestrator-coder-model-auth", "coder-api-key"),
                          ("orchestrator-openai-model-auth", "openai-api-key")):
            self.assertEqual(secrets[name]["data"], {key: ""})
        for document in documents:
            if document["kind"] not in ("Deployment", "Job"):
                continue
            for container in document["spec"]["template"]["spec"]["containers"]:
                for entry in container.get("env", []):
                    reference = entry.get("valueFrom", {}).get("secretKeyRef")
                    if reference:
                        self.assertIn(reference["key"], secrets[reference["name"]]["data"])
        worker = next(document for document in documents if document["kind"] == "Job")
        coder_key = next(entry for entry in worker["spec"]["template"]["spec"]["containers"][0]["env"]
                         if entry["name"] == "CODER_API_KEY")
        self.assertEqual(coder_key["valueFrom"]["secretKeyRef"], {
            "name": "orchestrator-coder-model-auth", "key": "coder-api-key",
        })

    def test_default_and_external_secret_modes(self):
        for chart in ("orchestrator", "inference"):
            for custom in (False, True):
                with self.subTest(chart=chart, custom=custom):
                    values = {}
                    name = "external-pull-secret" if custom else "ghcr-read-packages-pull-secret"
                    if custom:
                        target = values.setdefault("inference", {}) if chart == "inference" else values
                        target["imagePullSecrets"] = [{"name": name}]
                    documents = self.documents(chart, values)
                    self.assertFalse(any(document.get("type") == "kubernetes.io/dockerconfigjson"
                                         for document in documents))
                    self.assertEqual(any(document["kind"] == "Secret" and
                                         document["metadata"]["name"] == "orchestrator-gateway-auth"
                                         for document in documents), chart == "orchestrator")
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
                    self.assertNotIn("kind: Secret", result.stdout)

    def test_creation_reference_conflicts_fail(self):
        for chart in ("orchestrator", "inference"):
            values = {
                "registry": {"create": True, "username": USERNAME},
                "credentials": {"github": {"ghcrReadPackagesPat": TOKEN}},
            }
            target = values.setdefault("inference", {}) if chart == "inference" else values
            target["imagePullSecrets"] = [{"name": "external-pull-secret"}]
            result = self.render(chart, values)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires", result.stderr)

    def test_gateway_file_token_remains_separate(self):
        gateway_token = "gateway-only-sentinel-independent-32-characters"
        documents = self.documents("orchestrator", {
            "credentials": {
                "internal": {"gatewayClientToken": gateway_token},
                "models": {"openaiApiKey": "model-key-sentinel"},
            },
        })
        secrets = {document["metadata"]["name"]: document for document in documents
                   if document["kind"] == "Secret"}
        token = secrets["orchestrator-gateway-auth"]["data"]["gateway-auth-token"]
        self.assertEqual(base64.b64decode(token).decode(), gateway_token)
        model_key = secrets["orchestrator-openai-model-auth"]["data"]["openai-api-key"]
        self.assertEqual(base64.b64decode(model_key).decode(), "model-key-sentinel")
        result = self.render("orchestrator", {
            "credentials": {"internal": {"gatewayClientToken": "<replace-with-independent-token-32-characters>"}},
        })
        self.assertNotEqual(result.returncode, 0)

    def test_external_proxy_example(self):
        example = HELM_DIR / "orchestrator" / "example.values.yaml"
        contents = example.read_text()
        self.assertNotIn("utopiklab.com", contents)
        values = yaml.safe_load(contents)
        self.assertEqual(values["supervisor"]["config"]["acceptor-url"],
                         "https://gateway.example.com")
        self.assertFalse(values["management"]["enabled"])
        self.assertFalse(values["management"]["ingress"]["enabled"])
        self.assertEqual(values["management"]["publicOrigin"], "https://management.example.com")
        self.assertEqual(values["management"]["ingress"]["host"], "management.example.com")
        values["credentials"] = {
            "internal": {
                "gatewayClientToken": "gateway-proxy-example-dummy-token-32-chars",
                "supervisorServiceToken": "supervisor-proxy-example-dummy",
            },
            "github": {"pullRequestPat": "pr-proxy-example-dummy"},
        }
        documents = self.documents("orchestrator", values)
        ingress = next(document for document in documents if document["kind"] == "Ingress")
        self.assertEqual(ingress["spec"]["rules"][0]["host"], "orchestrator.local")
        self.assertNotIn("tls", ingress["spec"])
        self.assertEqual(ingress["metadata"]["annotations"][
            "traefik.ingress.kubernetes.io/router.entrypoints"], "web")

    def management_values(self, **ingress):
        defaults = yaml.safe_load((HELM_DIR / "orchestrator/values.yaml").read_text())
        return {"management": {
            "enabled": True,
            "authMode": "oidc",
            "image": defaults["gateway"]["image"],
            "publicOrigin": "https://management.example.com",
            "repositories": {"example/project": TOKEN},
            "oidc": {
                "issuer": "https://identity.example.com",
                "audience": "management",
                "jwksUrl": "https://identity.example.com/jwks",
                "allowedSubjects": ["operator"],
            },
            "ingress": {"enabled": True, "host": "management.example.com",
                        "tlsTermination": "internal", "tlsSecretName": "management-tls", **ingress},
        }}

    def test_management_admin_without_oidc(self):
        values = self.management_values()
        settings = values["management"]
        del settings["authMode"]
        del settings["oidc"]
        settings["admin"] = {"username": "admin", "password": "admin-password-test-sentinel"}
        documents = self.documents("orchestrator", values)
        secret = next(document for document in documents if document["kind"] == "Secret"
                      and document["metadata"]["name"] == "orchestrator-management-admin")
        self.assertEqual(base64.b64decode(secret["data"]["password"]).decode(), settings["admin"]["password"])
        deployment = next(document for document in documents if document["kind"] == "Deployment"
                          and document["metadata"]["name"] == "orchestrator-management")
        environment = {entry["name"]: entry for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(environment["MANAGEMENT_AUTH_MODE"]["value"], "admin")
        self.assertEqual(environment["MANAGEMENT_ADMIN_USERNAME"]["value"], "admin")
        self.assertEqual(environment["MANAGEMENT_ADMIN_PASSWORD"]["valueFrom"]["secretKeyRef"], {
            "name": secret["metadata"]["name"], "key": "password",
        })
        self.assertNotIn("MANAGEMENT_OIDC_ISSUER", environment)
        self.assertNotIn(settings["admin"]["password"], yaml.safe_dump(deployment))
        for password in ("", "short", "<replace-password>"):
            settings["admin"]["password"] = password
            result = self.render("orchestrator", values)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("password", result.stderr)
        settings["authMode"] = "none"
        self.assertNotEqual(self.render("orchestrator", values).returncode, 0)
        oidc_documents = self.documents("orchestrator", self.management_values())
        self.assertFalse(any(document["kind"] == "Secret" and document["metadata"]["name"]
                             == "orchestrator-management-admin" for document in oidc_documents))

    def test_management_managed_repository_credentials(self):
        values = self.management_values()
        repositories = {"example/project": TOKEN, "example/other": "other-sentinel"}
        values["management"]["repositories"] = repositories
        documents = self.documents("orchestrator", values)
        secret = next(document for document in documents if document["kind"] == "Secret"
                      and document["metadata"]["name"] == "orchestrator-management-repositories")
        self.assertEqual(secret["type"], "Opaque")
        self.assertEqual(secret["metadata"]["namespace"], "orchestrator")
        self.assertEqual(json.loads(base64.b64decode(secret["data"]["repositories.json"])), repositories)
        deployment = next(document for document in documents if document["kind"] == "Deployment"
                          and document["metadata"]["name"] == "orchestrator-management")
        pod = deployment["spec"]["template"]["spec"]
        volume = next(volume for volume in pod["volumes"] if volume["name"] == "repositories")
        self.assertEqual(volume["secret"]["secretName"], secret["metadata"]["name"])
        self.assertEqual(volume["secret"]["items"], [{"key": "repositories.json", "path": "repositories.json"}])
        environment = {entry["name"]: entry for entry in pod["containers"][0]["env"]}
        self.assertEqual(environment["CODEFACTORY_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"], {
            "name": "orchestrator-gateway-auth", "key": "gateway-auth-token",
        })
        for field in ("issuer", "audience", "jwksUrl", "allowedSubjects"):
            with self.subTest(field=field):
                original = values["management"]["oidc"][field]
                values["management"]["oidc"][field] = [] if field == "allowedSubjects" else ""
                result = self.render("orchestrator", values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(field, result.stderr)
                values["management"]["oidc"][field] = original

    def test_management_external_repository_secret_is_rejected(self):
        values = self.management_values()
        values["management"]["repositoriesSecret"] = "external-repositories"
        result = self.render("orchestrator", values)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("repositoriesSecret", result.stderr)

    def test_management_invalid_managed_repository_credentials_fail(self):
        for repositories in ({}, {"example/project": ""}, {"example/project": "has space"},
                             {"example/project": "<placeholder>"}, {"invalid": TOKEN},
                             {"../project": TOKEN}, {"example/..": TOKEN},
                             {"example/project": 123}):
            with self.subTest(repositories=repositories):
                values = self.management_values()
                values["management"]["repositories"] = repositories
                result = self.render("orchestrator", values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("values don't meet the specifications of the schema", result.stderr)

    def test_management_secrets_file_layering_and_lint(self):
        settings = self.management_values()["management"]
        identity = settings.pop("oidc")
        settings["oidc"] = {"issuer": "", "audience": "", "jwksUrl": "", "allowedSubjects": []}
        private = {"management": {"repositories": {"example/project": TOKEN}, "oidc": identity}}
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for name, values in (("shared.yaml", {"credentials": AUTH_CREDENTIALS}), ("orchestrator.yaml", {"management": settings}),
                                 ("secrets.yaml", private)):
                path = Path(directory) / name
                path.write_text(yaml.safe_dump(values))
                paths.append(path)
            documents = self.documents_from_value_files("orchestrator", paths)
            deployment = next(document for document in documents if document["kind"] == "Deployment"
                              and document["metadata"]["name"] == "orchestrator-management")
            environment = {entry["name"]: entry.get("value") for entry in
                           deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
            for name, field in (("MANAGEMENT_OIDC_ISSUER", "issuer"),
                                ("MANAGEMENT_OIDC_AUDIENCE", "audience"),
                                ("MANAGEMENT_OIDC_JWKS_URL", "jwksUrl")):
                self.assertEqual(environment[name], identity[field])
            self.assertEqual(environment["MANAGEMENT_ALLOWED_SUBJECTS"], "operator")
            self.assertTrue(any(document["kind"] == "Secret" and document["metadata"]["name"]
                                == "orchestrator-management-repositories" for document in documents))
            self.documents_from_value_files("inference", [paths[0], paths[2]])
            arguments = ["helm", "lint", str(HELM_DIR / "orchestrator")]
            for path in paths:
                arguments.extend(["--values", str(path)])
            result = subprocess.run(arguments, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, "Synthetic layered Helm lint failed")

    def test_management_managed_credentials_disabled(self):
        documents = self.documents("orchestrator", {"management": {
            "enabled": False, "repositories": {"example/project": TOKEN},
        }})
        self.assertFalse(any(document["kind"] == "Secret" and document["metadata"]["name"]
                             == "orchestrator-management-repositories" for document in documents))

    def test_management_invalid_oidc_settings_fail(self):
        for field, value in (("issuer", "http://identity.example.com"),
                             ("jwksUrl", "http://identity.example.com/jwks"),
                             ("allowedSubjects", [""]), ("allowedSubjects", ["operator,other"]),
                             ("allowedSubjects", ["operator", "operator"])):
            with self.subTest(field=field, value=value):
                values = self.management_values()
                values["management"]["repositories"] = {"example/project": TOKEN}
                values["management"]["oidc"][field] = value
                result = self.render("orchestrator", values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(field, result.stderr)

    def test_management_ingress_opt_in(self):
        disabled = self.management_values()
        disabled["management"]["enabled"] = False
        ingress_defaults = self.management_values()
        del ingress_defaults["management"]["ingress"]
        cases = ({}, disabled, {"management": {"ingress": {"enabled": True}}},
                 ingress_defaults, self.management_values(enabled=False))
        for values in cases:
            with self.subTest(values=values):
                documents = self.documents("orchestrator", values)
                ingresses = [document for document in documents if document["kind"] == "Ingress"]
                self.assertEqual(len(ingresses), 1)
                self.assertEqual(ingresses[0]["metadata"]["name"],
                                 "orchestrator-orchestrator-gateway-ingress")

    def test_management_ingress_routes_and_tls(self):
        for termination in ("internal", "external"):
            with self.subTest(termination=termination):
                values = self.management_values(tlsTermination=termination,
                                                tlsSecretName="management-tls" if termination == "internal" else "")
                documents = self.documents("orchestrator", values)
                ingress = next(document for document in documents if document["kind"] == "Ingress"
                               and document["metadata"]["name"] == "orchestrator-management-ingress")
                service = next(document for document in documents if document["kind"] == "Service"
                               and document["metadata"]["name"] == "orchestrator-management")
                self.assertEqual(ingress["apiVersion"], "networking.k8s.io/v1")
                self.assertEqual(ingress["metadata"]["namespace"], service["metadata"]["namespace"])
                rule = ingress["spec"]["rules"][0]
                self.assertEqual(rule["host"], "management.example.com")
                self.assertEqual(rule["http"]["paths"], [{
                    "path": "/", "pathType": "Prefix", "backend": {"service": {
                        "name": service["metadata"]["name"], "port": {"number": 8081},
                    }},
                }])
                self.assertEqual(service["spec"]["ports"][0]["port"], 8081)
                self.assertEqual(service["spec"]["ports"][0]["targetPort"], "http")
                self.assertEqual(service["spec"]["type"], "ClusterIP")
                deployment = next(document for document in documents if document["kind"] == "Deployment"
                                  and document["metadata"]["name"] == service["metadata"]["name"])
                environment = {entry["name"]: entry.get("value") for entry in
                               deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
                self.assertEqual(environment["MANAGEMENT_PUBLIC_ORIGIN"],
                                 values["management"]["publicOrigin"])
                annotations = ingress["metadata"]["annotations"]
                if termination == "internal":
                    self.assertEqual(annotations["traefik.ingress.kubernetes.io/router.entrypoints"], "websecure")
                    self.assertEqual(annotations["traefik.ingress.kubernetes.io/router.tls"], "true")
                    self.assertEqual(ingress["spec"]["tls"], [{
                        "hosts": [rule["host"]], "secretName": "management-tls",
                    }])
                else:
                    self.assertEqual(annotations["traefik.ingress.kubernetes.io/router.entrypoints"], "web")
                    self.assertNotIn("traefik.ingress.kubernetes.io/router.tls", annotations)
                    self.assertNotIn("tls", ingress["spec"])
                gateway = next(document for document in documents if document["kind"] == "Ingress"
                               and document["metadata"]["name"] == "orchestrator-orchestrator-gateway-ingress")
                self.assertEqual(gateway["spec"]["rules"][0]["http"]["paths"][0]["path"], "/api/v1")

    def test_management_ingress_invalid_settings_fail(self):
        for override in ({"host": ""}, {"host": "https://management.example.com"},
                         {"tlsTermination": "off"}, {"tlsSecretName": ""},
                         {"enabled": "true"}):
            with self.subTest(override=override):
                result = self.render("orchestrator", self.management_values(**override))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("management", result.stderr)
        for field, value in (("publicOrigin", "http://management.example.com"),
                             ("oidc", {"allowedSubjects": []})):
            values = self.management_values()
            values["management"][field] = value
            result = self.render("orchestrator", values)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("management", result.stderr)

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