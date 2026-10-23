import json
import time
from datetime import UTC, datetime
from unittest.mock import Mock

import github_auth
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from github_auth import (
    DISCOVERY_URL,
    ISSUER,
    JWKS_URL,
    READ_PERMISSIONS,
    AppUnavailable,
    AuthenticationError,
    AuthorizationError,
    GitHubAuth,
)


@pytest.fixture
def auth_setup(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file = tmp_path / "app.pem"
    key_file.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    policy = {"repository": "owner/repo", "repository_id": "123", "repository_owner_id": "456",
              "installation_id": "789", "bindings": [{"event_name": "issues", "ref": "refs/heads/main",
              "workflow_ref": "owner/repo/.github/workflows/drain.yaml@refs/heads/main",
              "sub": "repo:owner/repo:ref:refs/heads/main"}]}
    settings = {"GITHUB_OIDC_AUDIENCE": "https://gateway.example/tasks", "GITHUB_APP_ID": "12",
                "GITHUB_APP_PRIVATE_KEY_FILE": str(key_file), "GITHUB_REPOSITORY_POLICIES": json.dumps([policy])}
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if url == DISCOVERY_URL:
            return {"issuer": ISSUER, "jwks_uri": JWKS_URL}
        if url == JWKS_URL:
            return {"keys": [{**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), "kid": "test"}]}
        if url.endswith("/access_tokens"):
            return {"token": "test-installation-token", "permissions": {**kwargs["body"]["permissions"], "metadata": "read"},
                    "repositories": [{"id": 123}],
                    "expires_at": datetime.fromtimestamp(time.time() + 3600, UTC).isoformat()}
        if "/app/installations/" in url or url.endswith("/installation"):
            return {"id": 789, "app_id": 12, "account": {"id": 456}, "repository_selection": "selected", "suspended_at": None}
        return {"id": 123, "owner": {"id": 456}, "full_name": "owner/repo", "archived": False, "fork": False}

    claims = {"iss": ISSUER, "aud": settings["GITHUB_OIDC_AUDIENCE"], "iat": int(time.time()) - 1,
              "nbf": int(time.time()) - 1, "exp": int(time.time()) + 299,
              **{field: policy[field] for field in ("repository", "repository_id", "repository_owner_id")},
              **policy["bindings"][0]}
    return GitHubAuth(settings, fetch), key, claims, calls


@pytest.fixture(params=["selected", "all"])
def installation_auth(auth_setup, request):
    auth, _, _, _ = auth_setup
    original = auth.fetch

    def fetch(url, **kwargs):
        result = original(url, **kwargs)
        if url.endswith("/installation") or "/app/installations/" in url and not url.endswith("/access_tokens"):
            result["repository_selection"] = request.param
        return result

    auth.fetch = fetch
    return auth_setup


def test_verified_identity_and_scoped_app_token(installation_auth):
    auth, key, claims, calls = installation_auth
    policy, _ = auth.installation("owner/repo")
    assert policy["installation_id"] == "789"
    assert auth.authenticate(jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})) == "owner/repo"
    result = auth.mint("owner/repo")
    assert result["token"] == "test-installation-token"
    assert calls[-2][1]["body"] == {"repository_ids": [123], "permissions": READ_PERMISSIONS}
    assert all(url.startswith((ISSUER + "/", "https://api.github.com/")) for url, _ in calls)


@pytest.mark.parametrize("field,value,error", [
    ("iss", "https://attacker.example", AuthenticationError), ("aud", "other", AuthenticationError),
    ("exp", 1, AuthenticationError), ("iat", 9999999999, AuthenticationError),
    ("nbf", 9999999999, AuthenticationError), ("repository", "other/repo", AuthorizationError),
    ("repository_id", "999", AuthorizationError), ("repository_owner_id", "999", AuthorizationError),
    ("workflow_ref", "owner/repo/.github/workflows/evil.yaml@refs/heads/main", AuthorizationError),
    ("ref", "refs/heads/evil", AuthorizationError), ("sub", "repo:owner/repo:pull_request", AuthorizationError),
    ("event_name", "pull_request_target", AuthorizationError),
])
def test_claim_binding(auth_setup, field, value, error):
    auth, key, claims, _ = auth_setup
    claims[field] = value
    with pytest.raises(error):
        auth.authenticate(jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"}))


def test_signature_algorithm_and_size(auth_setup):
    auth, _, claims, _ = auth_setup
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for token in (jwt.encode(claims, other, algorithm="RS256", headers={"kid": "test"}),
                  jwt.encode(claims, "test-secret", algorithm="HS256", headers={"kid": "test"}), "x" * 16385):
        with pytest.raises(AuthenticationError):
            auth.authenticate(token)


def test_fail_closed_network_and_redaction(auth_setup):
    auth, key, claims, _ = auth_setup

    def failed(*args, **kwargs):
        raise OSError("sensitive-key-token-response")

    auth.fetch = failed
    with pytest.raises(AuthenticationError, match="^github-identity-invalid$"):
        auth.authenticate(jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"}))
    with pytest.raises(AppUnavailable, match="^github-app-unavailable$"):
        auth.mint("owner/repo")
    with pytest.raises(AuthorizationError):
        auth.mint("unknown/repo")


@pytest.mark.parametrize("stage", ["lookup", "mint"])
@pytest.mark.parametrize("selection", ["unknown", None, "missing"])
def test_installation_selection_fails_closed(auth_setup, stage, selection):
    auth, _, _, calls = auth_setup
    original = auth.fetch

    def fetch(url, **kwargs):
        result = original(url, **kwargs)
        if (stage == "lookup" and url.endswith("/installation")
                or stage == "mint" and url.endswith("/app/installations/789")):
            if selection == "missing":
                result.pop("repository_selection")
            else:
                result["repository_selection"] = selection
        return result

    auth.fetch = fetch
    with pytest.raises(AppUnavailable, match="^github-app-unavailable$"):
        if stage == "lookup":
            auth.installation("owner/repo")
        else:
            auth.mint("owner/repo")
    assert not any(url.endswith("/access_tokens") for url, _ in calls)


@pytest.mark.parametrize("kind", ["installation", "owner", "app", "suspended", "repository", "permissions", "expired", "token-repositories"])
def test_mint_rejects_wrong_installation_or_grant(installation_auth, kind):
    auth, _, _, _ = installation_auth
    original = auth.fetch

    def fetch(url, **kwargs):
        result = original(url, **kwargs)
        if url.endswith("/access_tokens"):
            if kind == "permissions":
                result["permissions"]["administration"] = "write"
            if kind == "expired":
                result["expires_at"] = "2000-01-01T00:00:00Z"
            if kind == "token-repositories":
                result["repositories"].append({"id": 999})
        elif "/app/installations/" in url:
            if kind == "installation":
                result["id"] = 999
            if kind == "owner":
                result["account"]["id"] = 999
            if kind == "app":
                result["app_id"] = 999
            if kind == "suspended":
                result["suspended_at"] = "2026-01-01T00:00:00Z"
        elif kind == "repository":
            result["id"] = 999
        return result
    auth.fetch = fetch
    with pytest.raises(AppUnavailable, match="^github-app-unavailable$"):
        auth.mint("owner/repo")


def test_jwks_cache_expires_and_network_failure_does_not_use_stale_keys(auth_setup):
    auth, key, claims, calls = auth_setup
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test", "jku": "https://attacker.test"})
    assert auth.authenticate(token) == "owner/repo"
    assert auth.authenticate(token) == "owner/repo"
    assert len(calls) == 2
    auth.keys_until = 0

    def unavailable(*args, **kwargs):
        raise OSError("unavailable")

    auth.fetch = unavailable
    with pytest.raises(AuthenticationError):
        auth.authenticate(token)


def test_discovery_cannot_choose_another_jwks_url(auth_setup):
    auth, key, claims, _ = auth_setup
    requested = []

    def discovery(url, **kwargs):
        requested.append(url)
        return {"issuer": ISSUER, "jwks_uri": "https://attacker.test/keys"}

    auth.fetch = discovery
    with pytest.raises(AuthenticationError):
        auth.authenticate(jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"}))
    assert requested == [DISCOVERY_URL]


@pytest.mark.parametrize("setting", ["GITHUB_AUTH_MODE", "CODEFACTORY_AUTH_MODE", "CODEFACTORY_AUTH_TOKEN",
                                     "GITHUB_TOKEN", "PR_GITHUB_TOKEN", "REPO_GITHUB_PAT"])
def test_obsolete_auth_settings_rejected(auth_setup, setting):
    auth, _, _, _ = auth_setup
    settings = {"GITHUB_OIDC_AUDIENCE": auth.audience, "GITHUB_APP_ID": auth.app_id,
                "GITHUB_APP_PRIVATE_KEY_FILE": auth.key_file,
                "GITHUB_REPOSITORY_POLICIES": json.dumps(list(auth.policies.values())), setting: "stale"}
    with pytest.raises(ValueError, match="^github-auth-configuration-invalid$"):
        GitHubAuth(settings)


@pytest.mark.parametrize("setting", ["GITHUB_AUTH_UNKNOWN", "GITHUB_APP_TOKEN", "GITHUB_OIDC_ISSUER", "CODEFACTORY_AUTH_UNKNOWN"])
def test_unknown_security_settings_rejected(auth_setup, setting):
    test_obsolete_auth_settings_rejected(auth_setup, setting)


@pytest.mark.parametrize("location", ["policy", "binding"])
def test_unknown_policy_security_fields_rejected(auth_setup, location):
    auth, _, _, _ = auth_setup
    policy = next(iter(auth.policies.values()))
    target = policy if location == "policy" else policy["bindings"][0]
    target["token"] = "unsupported-test-only"
    settings = {"GITHUB_OIDC_AUDIENCE": auth.audience, "GITHUB_APP_ID": auth.app_id,
                "GITHUB_APP_PRIVATE_KEY_FILE": auth.key_file, "GITHUB_REPOSITORY_POLICIES": json.dumps([policy])}
    with pytest.raises(ValueError, match="^github-auth-configuration-invalid$"):
        GitHubAuth(settings)


def test_app_configuration_is_mandatory():
    with pytest.raises(ValueError, match="^github-auth-configuration-invalid$"):
        GitHubAuth({})


def dynamic_auth(auth_setup, restricted=False):
    auth, key, claims, calls = auth_setup
    settings = {"GITHUB_OIDC_AUDIENCE": auth.audience, "GITHUB_APP_ID": auth.app_id,
                "GITHUB_APP_PRIVATE_KEY_FILE": auth.key_file,
                "GITHUB_WORKFLOW_POLICY": json.dumps({"ref": "refs/heads/main", "workflow": ".github/workflows/drain.yaml",
                                                      "events": ["issues", "workflow_dispatch"]})}
    if restricted:
        settings["GITHUB_REPOSITORY_POLICIES"] = json.dumps(list(auth.policies.values()))
    original = auth.fetch

    def fetch(url, **kwargs):
        if url == "https://api.github.com/user":
            return {"id": 42, "type": "User", "login": "verified-user"}
        if "/user/installations?" in url:
            return {"installations": [{"id": 789, "app_id": 12}]}
        result = original(url, **kwargs)
        if url == "https://api.github.com/repos/owner/repo":
            result["permissions"] = {"push": True}
        return result

    return GitHubAuth(settings, fetch), key, claims, calls


@pytest.mark.parametrize("restricted", [False, True])
def test_dynamic_actions_and_user_success(installation_auth, restricted):
    auth, key, claims, calls = dynamic_auth(installation_auth, restricted)
    for event in ("issues", "workflow_dispatch"):
        claims["event_name"] = event
        if restricted and event == "workflow_dispatch":
            continue
        assert auth.authenticate(jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})) == "owner/repo"
    assert auth.authorize_user("owner/repo", "ghu_test-only") == "42"
    assert auth.mint("owner/repo", write=True)["repo"] == "owner/repo"
    bodies = [kwargs["body"] for url, kwargs in calls if url.endswith("/access_tokens")]
    assert all(body.get("repository_ids") == [123] or body == {"repositories": ["repo"], "permissions": {"metadata": "read"}}
               for body in bodies)


@pytest.mark.parametrize("field,value", [
    ("repository_id", "999"), ("repository_owner_id", "999"),
    ("event_name", "pull_request"), ("event_name", "pull_request_target"), ("event_name", "push"),
    ("workflow_ref", "owner/repo/.github/workflows/evil.yaml@refs/heads/main"),
    ("ref", "refs/heads/untrusted"), ("sub", "repo:owner/repo:pull_request"),
])
def test_dynamic_actions_denies_untrusted_claims(auth_setup, field, value):
    auth, key, claims, _ = dynamic_auth(auth_setup)
    claims[field] = value
    with pytest.raises(AuthorizationError):
        auth.authenticate(jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"}))


@pytest.mark.parametrize("field,value,stage", [
    ("ref", "refs/heads/private-sensitive", "workflow-binding"),
    ("event_name", "private-sensitive", "workflow-binding"),
    ("workflow_ref", "private-sensitive", "workflow-binding"),
    ("sub", "repo:owner/repo:environment:private-sensitive", "workflow-binding"),
    ("repository_id", "private-sensitive", "repository-metadata"),
    ("repository_owner_id", "private-sensitive", "repository-metadata"),
])
def test_dynamic_denial_logs_only_stage_and_field(auth_setup, monkeypatch, field, value, stage):
    auth, key, claims, calls = dynamic_auth(auth_setup)
    claims[field] = value
    warning = Mock()
    monkeypatch.setattr(github_auth.logger, "warning", warning)
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})
    with pytest.raises(AuthorizationError, match="^github-workflow-not-authorized$"):
        auth.authenticate(token)
    warning.assert_called_once_with("github-authorization-denied stage=" + stage + " field=%s", field)
    assert value not in str(warning.call_args)
    assert token not in str(warning.call_args)
    if stage == "workflow-binding":
        assert [url for url, _ in calls] == [DISCOVERY_URL, JWKS_URL]


@pytest.mark.parametrize("kind", ["readonly", "revoked", "other-app", "other-installation", "owner", "fork", "archived", "api-error", "no-user"])
def test_user_access_fails_closed(auth_setup, kind):
    auth, _, _, _ = dynamic_auth(auth_setup)
    original = auth.fetch

    def fetch(url, **kwargs):
        if kind == "api-error":
            raise OSError("sensitive-token")
        result = original(url, **kwargs)
        if url.endswith("/user") and kind == "no-user":
            result["type"] = "Bot"
        if "/user/installations?" in url:
            if kind == "revoked":
                result["installations"] = []
            if kind == "other-app":
                result["installations"][0]["app_id"] = 999
            if kind == "other-installation":
                result["installations"][0]["id"] = 999
        if url.endswith("/repos/owner/repo"):
            if kind == "readonly":
                result["permissions"]["push"] = False
            if kind == "owner":
                result["owner"]["id"] = 999
            if kind in ("fork", "archived"):
                result[kind] = True
        return result

    auth.fetch = fetch
    with pytest.raises(AuthorizationError, match="^github-user-repository-denied$"):
        auth.authorize_user("owner/repo", "ghu_test-only")


@pytest.mark.parametrize("token", ["", "ghp_pat", "client-asserted-login", "ghu_invalid\n"])
def test_only_app_user_tokens_supported(auth_setup, token):
    auth, _, _, _ = dynamic_auth(auth_setup)
    with pytest.raises(AuthenticationError):
        auth.authorize_user("owner/repo", token)


def test_explicit_restriction_never_falls_back_to_dynamic(auth_setup):
    auth, key, claims, calls = dynamic_auth(auth_setup, restricted=True)
    for operation in (lambda: auth.authorize_user("other/repo", "ghu_test-only"),
                      lambda: auth.mint("other/repo")):
        with pytest.raises(AuthorizationError):
            operation()
    assert calls == []
    claims["repository"] = "other/repo"
    with pytest.raises(AuthorizationError):
        auth.authenticate(jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"}))