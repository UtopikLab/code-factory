import base64
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import jwt
import pytest
import yaml
from app import create_app
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from helm.test_registry import AUTH_CREDENTIALS


@pytest.fixture
def boundary(tmp_path):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    keys = Mock()
    keys.get_signing_key_from_jwt.return_value = SimpleNamespace(key=private.public_key())
    credentials = tmp_path / "repositories.json"
    credentials.write_text(json.dumps({"org/repo": "repository-test-secret"}))
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"version": "1", "catalog": "roles:\n  coder:\n    id: coder\n    endpoint_alias: coder\n    max_files: 8\n    tools: [repository_read]\n",
                                   "coder_model": "configured-coder", "judge_model": "configured-judge",
                                   "coder_native_download": True, "judge_native_download": False,
                                   "reviewers": "private", "coder_url": "https://secret@example.com"}))
    settings = {"MANAGEMENT_OIDC_ISSUER": "https://identity.example", "MANAGEMENT_OIDC_AUDIENCE": "management",
                "MANAGEMENT_OIDC_JWKS_URL": "https://identity.example/keys", "MANAGEMENT_ALLOWED_SUBJECTS": "operator",
                "MANAGEMENT_PUBLIC_ORIGIN": "https://factory.example", "MANAGEMENT_REPOSITORIES_FILE": str(credentials),
                "MANAGEMENT_RUNTIME_CONFIG_FILE": str(runtime), "MANAGEMENT_GATEWAY_URL": "https://gateway.example",
                "CODEFACTORY_AUTH_TOKEN": "gateway-test-secret"}
    upstream = Mock(return_value=httpx.Response(202, json={"task_id": "task-1", "status": "queued"}))
    client = TestClient(create_app(settings, httpx.MockTransport(upstream), keys))

    def token(**changes):
        return jwt.encode({"sub": "operator", "iss": "https://identity.example", "aud": "management",
                           "iat": int(time.time()), "exp": int(time.time()) + 120, **changes}, private, algorithm="RS256")

    client.headers.update({"Authorization": "Bearer " + token(), "Origin": "https://factory.example",
                           "X-Management-Request": "1"})
    return client, upstream, token, settings


def test_secure_defaults_and_all_routes_require_identity(boundary):
    client, upstream, _, _ = boundary
    client.headers.pop("Authorization")
    for path in ("/", "/assets/app.js", "/management/api/session", "/management/api/tasks", "/management/api/config"):
        assert client.get(path).status_code == 401
    assert client.post("/management/api/tasks", json={}).status_code == 401
    upstream.assert_not_called()
    closed = TestClient(create_app({}))
    assert closed.get("/management/api/tasks").status_code == 503
    assert closed.get("/").status_code == 503
    assert closed.get("/healthz").status_code == 200


@pytest.mark.parametrize("changes,code", [({"sub": "other"}, 403), ({"aud": "other"}, 401),
                                          ({"iss": "https://other"}, 401), ({"exp": 1}, 401)])
def test_signed_identity_and_operator_authorization(boundary, changes, code):
    client, upstream, token, _ = boundary
    client.headers["Authorization"] = "Bearer " + token(**changes)
    assert client.get("/management/api/session").status_code == code
    upstream.assert_not_called()


def test_unsigned_and_forged_identity_denied(boundary):
    client, upstream, _, _ = boundary
    client.headers["Authorization"] = "Bearer " + jwt.encode({"sub": "operator"}, key="", algorithm="none")
    client.headers["X-Auth-Request-User"] = "operator"
    assert client.get("/management/api/tasks").status_code == 401
    upstream.assert_not_called()


def test_submission_keeps_credentials_server_side_and_uses_contract(boundary):
    client, upstream, _, _ = boundary
    assert client.get("/management/api/repositories").json() == {"repositories": ["org/repo"]}
    response = client.post("/management/api/tasks", json={"repo": "ORG/repo", "issue_number": 7})
    assert response.status_code == 202
    request = upstream.call_args.args[0]
    assert request.url == "https://gateway.example/api/v1/tasks/spawn"
    assert json.loads(request.content) == {"repo": "org/repo", "issue_number": 7}
    assert request.headers["Authorization"] == "token gateway-test-secret"
    assert request.headers["X-Repository-Token"] == "repository-test-secret"
    assert "secret" not in response.text


@pytest.mark.parametrize("payload,code", [({"repo": "other/repo", "issue_number": 7}, 403),
    ({"repo": "org/repo", "issue_number": True}, 400), ({"repo": "org/repo", "issue_number": 0}, 400),
    ({"repo": "org/repo", "issue_number": 7, "token": "secret"}, 400), ([], 400)])
def test_submission_rejects_invalid_or_unapproved_input(boundary, payload, code):
    client, upstream, _, _ = boundary
    assert client.post("/management/api/tasks", json=payload).status_code == code
    upstream.assert_not_called()


def test_csrf_size_and_content_type_guards(boundary):
    client, upstream, _, _ = boundary
    assert client.post("/management/api/tasks", json={}, headers={"Origin": "https://attacker.example"}).status_code == 403
    client.headers.pop("X-Management-Request")
    assert client.post("/management/api/tasks", json={}).status_code == 403
    client.headers["X-Management-Request"] = "1"
    assert client.post("/management/api/tasks", content="{}").status_code == 415
    assert client.post("/management/api/tasks", content="x" * 4097,
                       headers={"Content-Type": "application/json"}).status_code == 413
    upstream.assert_not_called()


def test_task_and_config_summaries_are_allowlisted(boundary):
    client, upstream, _, _ = boundary
    upstream.return_value = httpx.Response(200, json={"task_id": "task-1", "repo": "org/repo", "status": "completed",
        "result": {"pr_url": "https://github.com/org/repo/pull/8", "patch": "private", "error": "secret"},
        "payload": "private", "job": {"env": "secret"}, "credential_ref": "secret"})
    detail = client.get("/management/api/tasks/task-1")
    assert detail.json()["pr_url"] == "https://github.com/org/repo/pull/8"
    assert "secret" not in detail.text and "private" not in detail.text
    upstream.return_value = httpx.Response(200, json={"task_id": "task-1", "repo": "org/repo",
                                                       "result": {"pr_url": "https://evil.example"}})
    assert client.get("/management/api/tasks/task-1").json()["pr_url"] is None
    config = client.get("/management/api/config")
    assert config.status_code == 200
    assert config.json()["models"][0]["model"] == "configured-coder"
    assert all(model["availability"] == "unknown" for model in config.json()["models"])
    assert "private" not in config.text and "example.com" not in config.text
    assert config.headers["Cache-Control"] == "no-store"


def test_listing_and_upstream_failures(boundary):
    client, upstream, _, _ = boundary
    upstream.return_value = httpx.Response(200, json={"tasks": [{"task_id": "task-1", "payload": "secret"}],
                                                     "total": 1, "limit": 25, "offset": 0})
    response = client.get("/management/api/tasks?limit=25&status=running")
    assert response.json()["tasks"] == [{"task_id": "task-1"}]
    assert upstream.call_args.args[0].url.params["status"] == "running"
    assert client.get("/management/api/tasks?token=no").status_code == 400
    upstream.return_value = httpx.Response(500, text="secret")
    response = client.get("/management/api/tasks/task-1")
    assert response.status_code == 502 and "secret" not in response.text
    upstream.return_value = httpx.Response(302, headers={"Location": "https://attacker.example"})
    assert client.get("/management/api/tasks/task-1").status_code == 502


def test_no_configuration_or_deployment_mutations(boundary):
    client, upstream, _, _ = boundary
    for path in ("/management/api/config", "/management/api/deploy", "/management/api/tasks/task-1/retry"):
        assert client.post(path, json={}).status_code == 405
    upstream.assert_not_called()


def test_adapter_to_real_gateway_contract(boundary, monkeypatch, tmp_path):
    _, _, _, settings = boundary
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
    import server as gateway
    monkeypatch.setattr(gateway, "TOKEN", "gateway-test-secret")
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setenv("GATEWAY_TRANSPORT_MODE", "local-http")
    monkeypatch.setattr(gateway, "_validate_repository", Mock())
    monkeypatch.setattr(gateway, "_create_task_secret", Mock(return_value="test-uid"))
    http_server = gateway.ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    settings["MANAGEMENT_GATEWAY_URL"] = f"http://127.0.0.1:{http_server.server_port}"
    from jwt import PyJWK
    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(jwt.algorithms.RSAAlgorithm.to_jwk(
        boundary[0].app.dependency_overrides.get("unused", rsa.generate_private_key(public_exponent=65537, key_size=2048)).public_key()))
    del public_key, PyJWK
    original_keys = boundary[0].app
    del original_keys
    try:
        app = create_app(settings)
        identity_dependency = next(route for route in app.routes if route.path == "/management/api/session").dependant.dependencies[0].call
        app.dependency_overrides[identity_dependency] = lambda: "operator"
        client = TestClient(app)
        client.headers.update({"Origin": settings["MANAGEMENT_PUBLIC_ORIGIN"], "X-Management-Request": "1"})
        accepted = client.post("/management/api/tasks", json={"repo": "org/repo", "issue_number": 7})
        assert accepted.status_code == 202
        task_id = accepted.json()["task_id"]
        assert client.get("/management/api/tasks").json()["tasks"][0]["task_id"] == task_id
        assert client.get("/management/api/tasks/" + task_id).json()["issue_number"] == 7
        assert client.post("/management/api/tasks", json={"repo": "org/repo", "issue_number": 7}).json()["task_id"] == task_id
        gateway._validate_repository.assert_called_with("org/repo", 7, "repository-test-secret")
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join()


def test_management_helm_opt_in_and_snapshot(tmp_path):
    chart = Path(__file__).resolve().parents[2] / "helm" / "orchestrator"
    values = tmp_path / "management.yaml"
    values.write_text(yaml.safe_dump({"credentials": AUTH_CREDENTIALS}))
    base = ["helm", "template", "test", str(chart), "-f", str(values)]
    default = subprocess.run(base, text=True, capture_output=True, check=True).stdout
    assert "component: management" not in default
    assert subprocess.run([*base, "--set", "management.enabled=true"], capture_output=True, check=False).returncode != 0
    values.write_text(yaml.safe_dump({"credentials": AUTH_CREDENTIALS,
        "management": {"enabled": True, "image": "example/management:test",
        "publicOrigin": "https://factory.example", "repositories": {"org/repo": "repository-test-secret"},
        "oidc": {"issuer": "https://identity.example", "audience": "management",
                 "jwksUrl": "https://identity.example/keys", "allowedSubjects": ["operator"]}}}))
    rendered = subprocess.run(base, text=True, capture_output=True, check=True).stdout
    documents = [document for document in yaml.safe_load_all(rendered) if document]
    deployment = next(document for document in documents if document["kind"] == "Deployment" and document["metadata"]["name"].endswith("-management"))
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True
    repositories = next(document for document in documents if document["kind"] == "Secret"
                        and document["metadata"]["name"] == "orchestrator-management-repositories")
    assert json.loads(base64.b64decode(repositories["data"]["repositories.json"])) == {"org/repo": "repository-test-secret"}
    assert pod["volumes"][0]["secret"]["secretName"] == repositories["metadata"]["name"]
    assert not any(document["kind"] == "Ingress" and "management" in document["metadata"]["name"] for document in documents)
    snapshot = next(document for document in documents if document["kind"] == "ConfigMap" and "runtime-config.json" in document.get("data", {}))
    runtime = json.loads(snapshot["data"]["runtime-config.json"])
    assert runtime["coder_model"] and runtime["judge_model"]
    assert pod["volumes"][1]["configMap"]["name"] == snapshot["metadata"]["name"]