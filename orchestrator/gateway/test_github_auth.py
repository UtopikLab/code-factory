import json
import time
from datetime import UTC, datetime

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
        if "/app/installations/" in url:
            return {"id": 789, "app_id": 12, "account": {"id": 456}, "repository_selection": "selected", "suspended_at": None}
        return {"id": 123, "owner": {"id": 456}, "full_name": "owner/repo", "archived": False}

    claims = {"iss": ISSUER, "aud": settings["GITHUB_OIDC_AUDIENCE"], "iat": int(time.time()) - 1,
              "nbf": int(time.time()) - 1, "exp": int(time.time()) + 299,
              **{field: policy[field] for field in ("repository", "repository_id", "repository_owner_id")},
              **policy["bindings"][0]}
    return GitHubAuth(settings, fetch), key, claims, calls


def test_verified_identity_and_scoped_app_token(auth_setup):
    auth, key, claims, calls = auth_setup
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


@pytest.mark.parametrize("kind", ["installation", "owner", "app", "all-repos", "suspended", "repository", "permissions", "expired", "token-repositories"])
def test_mint_rejects_wrong_installation_or_grant(auth_setup, kind):
    auth, _, _, _ = auth_setup
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
            if kind == "all-repos":
                result["repository_selection"] = "all"
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