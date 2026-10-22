import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
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

AUTH_CREDENTIALS = {
    "internal": {
        "supervisorServiceToken": "supervisor-test-sentinel",
    },
}
APP_VALUES = yaml.safe_load((Path(__file__).resolve().parents[2] / "helm/orchestrator/example.values.yaml").read_text())["githubAuth"]


def test_module_import_does_not_construct_app_with_ci_environment():
    environment = {**os.environ, "GITHUB_TOKEN": "ci-test-only"}
    subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=Path(__file__).parent, env=environment,
        capture_output=True, text=True, check=True,
    )
    result = subprocess.run(
        [sys.executable, "-c", "import app; app.create_app()"],
        cwd=Path(__file__).parent,
        env=environment,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "repository-authentication-configuration-invalid" in result.stderr


@pytest.fixture
def boundary(tmp_path):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    keys = Mock()
    keys.get_signing_key_from_jwt.return_value = SimpleNamespace(key=private.public_key())
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"version": "1", "catalog": "roles:\n  coder:\n    id: coder\n    endpoint_alias: coder\n    max_files: 8\n    tools: [repository_read]\n",
                                   "coder_model": "configured-coder", "judge_model": "configured-judge",
                                   "coder_native_download": True, "judge_native_download": False,
                                   "reviewers": "private", "coder_url": "https://secret@example.com"}))
    settings = {"MANAGEMENT_AUTH_MODE": "oidc",
                "MANAGEMENT_OIDC_ISSUER": "https://identity.example", "MANAGEMENT_OIDC_AUDIENCE": "management",
                "MANAGEMENT_OIDC_JWKS_URL": "https://identity.example/keys", "MANAGEMENT_ALLOWED_SUBJECTS": "operator",
                "MANAGEMENT_PUBLIC_ORIGIN": "https://factory.example",
                "GITHUB_REPOSITORY_POLICIES": json.dumps([{"repository": "org/repo"}]),
                "MANAGEMENT_RUNTIME_CONFIG_FILE": str(runtime), "MANAGEMENT_GATEWAY_URL": "https://gateway.example",
                "MANAGEMENT_SERVICE_TOKEN": "management-service-test-only-at-least-32-characters"}
    upstream = Mock(return_value=httpx.Response(202, json={"task_id": "task-1", "status": "queued"}))
    client = TestClient(create_app(settings, httpx.MockTransport(upstream), keys))

    def token(**changes):
        return jwt.encode({"sub": "operator", "iss": "https://identity.example", "aud": "management",
                           "iat": int(time.time()), "exp": int(time.time()) + 120, **changes}, private, algorithm="RS256")

    client.headers.update({"Authorization": "Bearer " + token(), "Origin": "https://factory.example",
                           "X-Management-Request": "1"})
    return client, upstream, token, settings


def test_admin_login_without_oidc(boundary, tmp_path):
    _, upstream, _, settings = boundary
    settings.update({"MANAGEMENT_AUTH_MODE": "admin", "MANAGEMENT_ADMIN_PASSWORD": "admin-test-password-only"})
    for key in list(settings):
        if key.startswith("MANAGEMENT_OIDC_") or key == "MANAGEMENT_ALLOWED_SUBJECTS":
            del settings[key]
    (tmp_path / "index.html").write_text("<html>Management</html>")
    settings["MANAGEMENT_STATIC_DIR"] = str(tmp_path)
    client = TestClient(create_app(settings, httpx.MockTransport(upstream)))
    for path in ("/", "/management/api/session", "/management/api/tasks"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic ")
        assert client.get(path, auth=("admin", "wrong-password")).status_code == 401
        assert client.get(path, auth=("other", "admin-test-password-only")).status_code == 401
    client.auth = ("admin", "admin-test-password-only")
    assert client.get("/").status_code == 200
    assert client.get("/management/api/session").json() == {"subject": "admin"}
    assert "admin-test-password-only" not in client.get("/management/api/session").text
    assert client.post("/management/api/tasks", json={}).status_code == 403
    response = client.post("/management/api/tasks", json={"repo": "org/repo", "issue_number": 7},
                           headers={"Origin": settings["MANAGEMENT_PUBLIC_ORIGIN"], "X-Management-Request": "1"})
    assert response.status_code == 202
    assert upstream.call_args.args[0].headers["Authorization"] == "token management-service-test-only-at-least-32-characters"
    for password in ("", "short!", "abcdefgh", "abcdefgh ", "abcdefg<", "abcdef! "):
        settings["MANAGEMENT_ADMIN_PASSWORD"] = password
        assert TestClient(create_app(settings)).get("/management/api/session").status_code == 503
    settings["MANAGEMENT_AUTH_MODE"] = "unknown"
    assert TestClient(create_app(settings)).get("/management/api/session").status_code == 503


def test_app_submission_uses_policy_not_repository_pat_mapping(boundary):
    _, upstream, _, settings = boundary
    settings.update({"MANAGEMENT_SERVICE_TOKEN": "management-service-only",
                     "GITHUB_REPOSITORY_POLICIES": json.dumps([{"repository": "org/repo"}])})
    settings.update({"MANAGEMENT_AUTH_MODE": "admin", "MANAGEMENT_ADMIN_PASSWORD": "admin-test-password-only"})
    client = TestClient(create_app(settings, httpx.MockTransport(upstream)))
    client.auth = ("admin", "admin-test-password-only")
    assert client.get("/management/api/repositories").json() == {"repositories": ["org/repo"]}
    response = client.post("/management/api/tasks", json={"repo": "org/repo", "issue_number": 7},
                           headers={"Origin": settings["MANAGEMENT_PUBLIC_ORIGIN"], "X-Management-Request": "1"})
    assert response.status_code == 202
    request = upstream.call_args.args[0]
    assert request.headers["Authorization"] == "token management-service-only"
    assert "X-Repository-Token" not in request.headers
    assert "repository-test-secret" not in request.content.decode()
    assert client.post("/management/api/tasks", json={"repo": "other/repo", "issue_number": 7},
                       headers={"Origin": settings["MANAGEMENT_PUBLIC_ORIGIN"], "X-Management-Request": "1"}).status_code == 403


@pytest.mark.parametrize("setting", ["MANAGEMENT_REPOSITORIES_FILE", "CODEFACTORY_AUTH_TOKEN", "GITHUB_AUTH_MODE",
                                     "REPO_GITHUB_PAT", "GITHUB_APP_PRIVATE_KEY_FILE", "GITHUB_OIDC_ISSUER"])
def test_management_rejects_obsolete_or_gateway_only_auth_settings(boundary, setting):
    settings = {**boundary[3], setting: "stale-test-only"}
    with pytest.raises(ValueError, match="^repository-authentication-configuration-invalid$"):
        create_app(settings)


@pytest.mark.parametrize("password", ["abcdefg!", "elevenchar!"])
def test_admin_password_accepts_eight_characters_with_special_character(boundary, password):
    _, _, _, settings = boundary
    settings.update({"MANAGEMENT_AUTH_MODE": "admin", "MANAGEMENT_ADMIN_PASSWORD": password})
    client = TestClient(create_app(settings))
    assert client.get("/management/api/session", auth=("admin", password)).json() == {"subject": "admin"}


@pytest.mark.parametrize("repository_config", ["absent", "empty"])
def test_admin_operations_without_repository_access(boundary, tmp_path, repository_config):
    _, upstream, _, settings = boundary
    settings.update({"MANAGEMENT_AUTH_MODE": "admin", "MANAGEMENT_ADMIN_PASSWORD": "abcdefg!"})
    if repository_config == "absent":
        settings.pop("GITHUB_REPOSITORY_POLICIES")
    else:
        settings["GITHUB_REPOSITORY_POLICIES"] = "[]"
    (tmp_path / "index.html").write_text("<html>Management</html>")
    settings["MANAGEMENT_STATIC_DIR"] = str(tmp_path)
    client = TestClient(create_app(settings, httpx.MockTransport(upstream)))
    client.auth = ("admin", "abcdefg!")
    for path in ("/", "/management/api/session", "/management/api/config"):
        assert client.get(path).status_code == 200
    assert client.get("/management/api/repositories").status_code == 503
    upstream.assert_not_called()
    upstream.return_value = httpx.Response(200, json={"tasks": [{"task_id": "task-1", "patch": "private"}],
                                                    "total": 1, "limit": 25, "offset": 0})
    assert client.get("/management/api/tasks").json()["tasks"] == [{"task_id": "task-1"}]
    upstream.return_value = httpx.Response(200, json={"task_id": "task-1", "result": {"patch": "private"}})
    assert client.get("/management/api/tasks/task-1").json() == {"task_id": "task-1", "pr_url": None}
    upstream.reset_mock()
    response = client.post("/management/api/tasks", json={"repo": "org/repo", "issue_number": 7},
                           headers={"Origin": settings["MANAGEMENT_PUBLIC_ORIGIN"], "X-Management-Request": "1"})
    assert response.status_code == 503
    upstream.assert_not_called()


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
    assert request.headers["Authorization"] == "token management-service-test-only-at-least-32-characters"
    assert "X-Repository-Token" not in request.headers
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


@pytest.mark.parametrize("auth_mode", ["admin", "oidc"])
@pytest.mark.parametrize("github_status", [200, 401, 403, 404, 503])
def test_adapter_to_real_gateway_contract(boundary, monkeypatch, tmp_path, auth_mode, github_status):
    _, _, _, settings = boundary
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
    import server as gateway
    monkeypatch.setenv("MANAGEMENT_SERVICE_TOKEN", settings["MANAGEMENT_SERVICE_TOKEN"])
    app_auth = Mock()
    app_auth.policy.return_value = {"repository": "org/repo"}
    app_auth.mint.return_value = {"token": "repository-test-secret"}
    monkeypatch.setattr(gateway, "_github_app", lambda: app_auth)
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setenv("GATEWAY_TRANSPORT_MODE", "local-http")
    def github(method, path, *, token):
        assert method == "GET" and token == "repository-test-secret"
        if path == "/repos/org/repo":
            return {"full_name": "org/repo", "permissions": {"pull": True}}
        if github_status != 200:
            raise urllib.error.HTTPError("https://api.github.com" + path, github_status, "denied", {}, None)
        if path == "/repos/org/repo/issues/7":
            return {"number": 7, "html_url": "https://github.com/org/repo/issues/7"}
        return []

    monkeypatch.setattr(gateway, "_github", Mock(side_effect=github))
    monkeypatch.setattr(gateway, "_validate_repository", Mock(wraps=gateway._validate_repository))
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
        settings.update({"MANAGEMENT_AUTH_MODE": auth_mode, "MANAGEMENT_ADMIN_PASSWORD": "abcdefg!"})
        app = create_app(settings)
        identity_dependency = next(route for route in app.routes if route.path == "/management/api/session").dependant.dependencies[0].call
        if auth_mode == "oidc":
            app.dependency_overrides[identity_dependency] = lambda: "operator"
        client = TestClient(app)
        if auth_mode == "admin":
            client.auth = ("admin", "abcdefg!")
        client.headers.update({"Origin": settings["MANAGEMENT_PUBLIC_ORIGIN"], "X-Management-Request": "1"})
        accepted = client.post("/management/api/tasks", json={"repo": "org/repo", "issue_number": 7})
        if github_status != 200:
            assert accepted.status_code == 403
            gateway._create_task_secret.assert_not_called()
            assert client.get("/management/api/tasks").json()["total"] == 0
            return
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


@pytest.mark.parametrize("command", ["template", "lint"])
@pytest.mark.parametrize("gateway_url,shared", [("", True), ("https://factory.example/", True),
                                               ("https://gateway.example", False)])
def test_management_helm_shared_origin_routes_gateway(tmp_path, command, gateway_url, shared):
    chart = Path(__file__).resolve().parents[2] / "helm" / "orchestrator"
    values = tmp_path / "management.yaml"
    values.write_text(yaml.safe_dump({"credentials": AUTH_CREDENTIALS, "githubAuth": APP_VALUES,
        "supervisor": {"config": {"acceptor-url": "https://factory.example"}},
        "management": {"enabled": True, "publicOrigin": "https://factory.example",
                       "gatewayUrl": gateway_url, "admin": {"password": "abcdefg!"},
                       "ingress": {"enabled": True, "host": "factory.example",
                                   "tlsTermination": "external"}}}))
    arguments = ["helm", command, *(["test"] if command == "template" else []), str(chart), "-f", str(values)]
    rendered = subprocess.run(arguments, text=True, capture_output=True, check=True).stdout
    if command == "lint":
        return
    ingress = next(document for document in yaml.safe_load_all(rendered) if document
                   and document["kind"] == "Ingress" and document["metadata"]["name"].endswith("-management-ingress"))
    paths = {path["path"]: path["backend"]["service"] for path in ingress["spec"]["rules"][0]["http"]["paths"]}
    assert paths["/"] == {"name": "orchestrator-management", "port": {"number": 8081}}
    assert ("/api/v1" in paths) is shared
    if shared:
        assert paths["/api/v1"] == {"name": "orchestrator-orchestrator-gateway", "port": {"number": 8000}}


def test_management_helm_opt_in_and_snapshot(tmp_path):
    chart = Path(__file__).resolve().parents[2] / "helm" / "orchestrator"
    values = tmp_path / "management.yaml"
    values.write_text(yaml.safe_dump({"credentials": AUTH_CREDENTIALS, "githubAuth": APP_VALUES}))
    base = ["helm", "template", "test", str(chart), "-f", str(values)]
    default = subprocess.run(base, text=True, capture_output=True, check=True).stdout
    assert "component: management" not in default
    assert subprocess.run([*base, "--set", "management.enabled=true"], capture_output=True, check=False).returncode != 0
    values.write_text(yaml.safe_dump({"credentials": AUTH_CREDENTIALS, "githubAuth": APP_VALUES,
        "management": {"enabled": True, "authMode": "oidc", "image": "example/management:test",
        "publicOrigin": "https://factory.example",
        "oidc": {"issuer": "https://identity.example", "audience": "management",
                 "jwksUrl": "https://identity.example/keys", "allowedSubjects": ["operator"]}}}))
    rendered = subprocess.run(base, text=True, capture_output=True, check=True).stdout
    documents = [document for document in yaml.safe_load_all(rendered) if document]
    deployment = next(document for document in documents if document["kind"] == "Deployment" and document["metadata"]["name"].endswith("-management"))
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True
    assert not any(document["kind"] == "Secret" and document["metadata"]["name"] == "orchestrator-management-repositories"
                   for document in documents)
    assert not any(document["kind"] == "Ingress" and "management" in document["metadata"]["name"] for document in documents)
    snapshot = next(document for document in documents if document["kind"] == "ConfigMap" and "runtime-config.json" in document.get("data", {}))
    runtime = json.loads(snapshot["data"]["runtime-config.json"])
    assert runtime["coder_model"] and runtime["judge_model"]
    assert pod["volumes"][0]["configMap"]["name"] == snapshot["metadata"]["name"]


@pytest.mark.parametrize("command", ["template", "lint"])
@pytest.mark.parametrize("password,valid", [
    ("abcdefg!", True), ("elevenchar!", True), ("short!", False),
    ("abcdefgh", False), ("abcdefghijklmnop", False), ("abcdef! ", False), ("abcdefg<", False),
])
def test_management_helm_admin_password(tmp_path, command, password, valid):
    chart = Path(__file__).resolve().parents[2] / "helm" / "orchestrator"
    values = tmp_path / "management.yaml"
    values.write_text(yaml.safe_dump({"credentials": AUTH_CREDENTIALS, "githubAuth": APP_VALUES, "management": {
        "enabled": True, "publicOrigin": "https://factory.example",
        "admin": {"username": "admin", "password": password}}}))
    arguments = ["helm", command, *(["test"] if command == "template" else []), str(chart), "-f", str(values)]
    result = subprocess.run(arguments, text=True, capture_output=True, check=False)
    assert (result.returncode == 0) == valid, result.stderr + result.stdout
    if valid and command == "template":
        documents = [document for document in yaml.safe_load_all(result.stdout) if document]
        admin_secret = next(document for document in documents if document["kind"] == "Secret"
                            and document["metadata"]["name"] == "orchestrator-management-admin")
        assert base64.b64decode(admin_secret["data"]["password"]).decode() == password