"""GitHub Actions identity and repository-scoped App credentials."""

import json
import re
import threading
import time
import urllib.request
from datetime import datetime

import jwt

ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = ISSUER + "/.well-known/jwks"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
READ_PERMISSIONS = {"contents": "read", "issues": "read", "pull_requests": "read"}
WRITE_PERMISSIONS = {"contents": "write", "issues": "read", "pull_requests": "write"}


class AuthenticationError(Exception):
    pass


class AuthorizationError(Exception):
    pass


class AppUnavailable(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect-rejected")


def trusted_json(url, method="GET", token=None, body=None):
    headers = {"Accept": "application/vnd.github+json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
        raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("response-too-large")
        return json.loads(raw)


class GitHubAuth:
    def __init__(self, settings, fetch=trusted_json, clock=time.time):
        self.fetch, self.clock = fetch, clock
        self.audience = settings.get("GITHUB_OIDC_AUDIENCE", "")
        self.app_id = settings.get("GITHUB_APP_ID", "")
        self.key_file = settings.get("GITHUB_APP_PRIVATE_KEY_FILE", "")
        self.lock = threading.Lock()
        self.keys, self.keys_until = {}, 0
        try:
            policies = json.loads(settings.get("GITHUB_REPOSITORY_POLICIES", "[]"))
            if not self.audience or not re.fullmatch(r"[1-9][0-9]*", self.app_id) or not self.key_file:
                raise ValueError()
            if not isinstance(policies, list) or not policies or len(policies) > 100:
                raise ValueError()
            self.policies = {}
            for policy in policies:
                repo = policy["repository"]
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(
                        part in (".", "..") for part in repo.split("/")):
                    raise ValueError()
                for field in ("repository_id", "repository_owner_id", "installation_id"):
                    if not isinstance(policy[field], str) or not re.fullmatch(r"[1-9][0-9]*", policy[field]):
                        raise ValueError()
                if not isinstance(policy["bindings"], list) or not policy["bindings"]:
                    raise ValueError()
                for binding in policy["bindings"]:
                    if (binding["event_name"] not in ("issues", "workflow_dispatch")
                            or not isinstance(binding["ref"], str) or not binding["ref"].startswith("refs/heads/")
                            or binding["workflow_ref"] != repo + "/.github/workflows/drain.yaml@" + binding["ref"]
                            or binding["sub"] != "repo:" + repo + ":ref:" + binding["ref"]):
                        raise ValueError()
                if repo.lower() in self.policies:
                    raise ValueError()
                self.policies[repo.lower()] = policy
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("github-auth-configuration-invalid") from None

    def policy(self, repo):
        policy = self.policies.get((repo or "").lower())
        if policy is None:
            raise AuthorizationError("repository-not-authorized")
        return policy

    def authenticate(self, token):
        try:
            if not token or len(token) > 16384:
                raise ValueError()
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or len(header["kid"]) > 256:
                raise ValueError()
            with self.lock:
                if self.clock() >= self.keys_until:
                    discovery = self.fetch(DISCOVERY_URL)
                    if discovery.get("issuer") != ISSUER or discovery.get("jwks_uri") != JWKS_URL:
                        raise ValueError()
                    document = self.fetch(JWKS_URL)
                    if not isinstance(document.get("keys"), list) or not 1 <= len(document["keys"]) <= 32:
                        raise ValueError()
                    keys = {}
                    for key in document["keys"]:
                        if key.get("kty") == "RSA" and key.get("use", "sig") == "sig" and key.get("alg", "RS256") == "RS256":
                            keys[key["kid"]] = jwt.PyJWK.from_dict(key, algorithm="RS256").key
                    self.keys, self.keys_until = keys, self.clock() + 300
                key = self.keys[header["kid"]]
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=ISSUER, audience=self.audience,
                                options={"strict_aud": True, "require": ["iss", "aud", "exp", "iat", "nbf", "sub", "repository",
                                                     "repository_id", "repository_owner_id", "workflow_ref", "ref", "event_name"]})
            if (any(type(claims[field]) is not int for field in ("exp", "iat", "nbf"))
                    or not 0 < claims["exp"] - claims["iat"] <= 600):
                raise ValueError()
        except (jwt.PyJWTError, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise AuthenticationError("github-identity-invalid") from None
        policy = self.policy(claims["repository"] if isinstance(claims["repository"], str) else None)
        if (claims["repository"] != policy["repository"]
                or claims["repository_id"] != policy["repository_id"]
                or claims["repository_owner_id"] != policy["repository_owner_id"]
                or not any(all(claims[field] == binding[field] for field in
                               ("workflow_ref", "ref", "sub", "event_name")) for binding in policy["bindings"])):
            raise AuthorizationError("github-workflow-not-authorized")
        return policy["repository"]

    def mint(self, repo, write=False):
        policy = self.policy(repo)
        try:
            with open(self.key_file, encoding="utf-8") as source:
                private_key = source.read(32769)
            if len(private_key) > 32768:
                raise ValueError()
            now = int(self.clock())
            assertion = jwt.encode({"iat": now - 30, "exp": now + 300, "iss": self.app_id}, private_key, algorithm="RS256")
            installation = self.fetch("https://api.github.com/app/installations/" + policy["installation_id"], token=assertion)
            if (str(installation.get("id")) != policy["installation_id"]
                    or str(installation.get("app_id")) != self.app_id
                    or str(installation.get("account", {}).get("id")) != policy["repository_owner_id"]
                    or installation.get("repository_selection") != "selected" or installation.get("suspended_at") is not None):
                raise ValueError()
            permissions = WRITE_PERMISSIONS if write else READ_PERMISSIONS
            result = self.fetch("https://api.github.com/app/installations/" + policy["installation_id"] + "/access_tokens",
                                method="POST", token=assertion,
                                body={"repository_ids": [int(policy["repository_id"])], "permissions": permissions})
            token = result["token"]
            expires = datetime.fromisoformat(result["expires_at"]).timestamp()
            granted = dict(result.get("permissions", {}))
            metadata_permission = granted.pop("metadata", "read")
            if (not isinstance(token, str) or not token or len(token) > 4096 or any(char.isspace() for char in token)
                    or not now + 60 < expires <= now + 3660 or granted != permissions or metadata_permission != "read"
                    or [str(repository.get("id")) for repository in result["repositories"]] != [policy["repository_id"]]):
                raise ValueError()
            metadata = self.fetch("https://api.github.com/repos/" + policy["repository"], token=token)
            if (str(metadata.get("id")) != policy["repository_id"]
                    or str(metadata.get("owner", {}).get("id")) != policy["repository_owner_id"]
                    or metadata.get("full_name", "").lower() != policy["repository"].lower() or metadata.get("archived")):
                raise ValueError()
            return {"token": token, "expires_at": expires, "repo": policy["repository"]}
        except (jwt.PyJWTError, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise AppUnavailable("github-app-unavailable") from None