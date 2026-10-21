import json
import os
import re
import secrets
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import httpx
import jwt
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic

TASK_FIELDS = ("task_id", "repo", "issue_number", "status", "lifecycle", "credential_cleanup",
               "retry_of", "retry_task_id", "attempts", "config_version", "config_hash")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
TASK_ID = re.compile(r"[A-Za-z0-9_.-]{1,63}")


def create_app(settings=None, transport=None, signing_keys=None):
    settings = dict(os.environ if settings is None else settings)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    issuer = settings.get("MANAGEMENT_OIDC_ISSUER", "")
    audience = settings.get("MANAGEMENT_OIDC_AUDIENCE", "")
    jwks_url = settings.get("MANAGEMENT_OIDC_JWKS_URL", "")
    subjects = set(filter(None, settings.get("MANAGEMENT_ALLOWED_SUBJECTS", "").split(",")))
    origin = settings.get("MANAGEMENT_PUBLIC_ORIGIN", "")
    auth_mode = settings.get("MANAGEMENT_AUTH_MODE", "admin")
    admin_username = settings.get("MANAGEMENT_ADMIN_USERNAME", "admin")
    admin_password = settings.get("MANAGEMENT_ADMIN_PASSWORD", "")
    basic = HTTPBasic(auto_error=False, realm="Code Factory Admin")
    keys = signing_keys
    if keys is None and jwks_url.startswith("https://"):
        keys = jwt.PyJWKClient(jwks_url, timeout=5)

    async def identity(request: Request):
        if auth_mode == "admin":
            if (not origin or not admin_username or len(admin_password) < 8
                    or re.search(r"[<>\s]", admin_password)
                    or not re.search(r"[^A-Za-z0-9<>\s]", admin_password)):
                raise HTTPException(503, "admin-not-configured")
            credentials = await basic(request)
            if credentials is not None:
                username_valid = secrets.compare_digest(credentials.username.encode(), admin_username.encode())
                password_valid = secrets.compare_digest(credentials.password.encode(), admin_password.encode())
                if username_valid and password_valid:
                    return admin_username
            raise HTTPException(401, "identity-required", headers={"WWW-Authenticate": 'Basic realm="Code Factory Admin"'})
        if auth_mode != "oidc":
            raise HTTPException(503, "identity-not-configured")
        if not (issuer.startswith("https://") and audience and keys and subjects and origin):
            raise HTTPException(503, "identity-not-configured")
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > 16384:
            raise HTTPException(401, "identity-required")
        token = authorization[7:]
        try:
            key = keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=audience, issuer=issuer,
                                options={"require": ["exp", "iat", "sub", "iss", "aud"]})
        except (jwt.PyJWTError, ValueError, OSError):
            raise HTTPException(401, "identity-invalid") from None
        if claims["sub"] not in subjects:
            raise HTTPException(403, "operator-not-authorized")
        return claims["sub"]

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    def repositories():
        if not settings.get("MANAGEMENT_REPOSITORIES_FILE"):
            return {}
        try:
            data = json.loads(Path(settings["MANAGEMENT_REPOSITORIES_FILE"]).read_text())
            if not isinstance(data, dict) or any(
                not REPOSITORY.fullmatch(repo) or any(part in (".", "..") for part in repo.split("/"))
                or not isinstance(token, str) or not token or any(char.isspace() for char in token)
                for repo, token in data.items()
            ):
                raise ValueError()
            return data
        except (KeyError, OSError, ValueError, TypeError):
            raise HTTPException(503, "repository-credentials-not-configured") from None

    async def gateway(method, path, *, params=None, payload=None, repository_token=None):
        base = settings.get("MANAGEMENT_GATEWAY_URL", "")
        token = settings.get("CODEFACTORY_AUTH_TOKEN", "")
        parsed = urlsplit(base)
        local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if (not token or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme != "https" and not (parsed.scheme == "http" and local))):
            raise HTTPException(503, "gateway-not-configured")
        headers = {"Authorization": "token " + token}
        if repository_token:
            headers["X-Repository-Token"] = repository_token
        try:
            async with httpx.AsyncClient(transport=transport, timeout=35, follow_redirects=False) as client:
                response = await client.request(method, base.rstrip("/") + path, params=params,
                                                json=payload, headers=headers)
            if response.status_code not in (200, 202):
                status = response.status_code if response.status_code in (400, 403, 404, 409) else 502
                raise HTTPException(status, "gateway-request-rejected" if status != 502 else "gateway-unavailable")
            return response.status_code, response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, "gateway-unavailable") from None

    def task_summary(task):
        return {field: task[field] for field in TASK_FIELDS if field in task}

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/management/api/session")
    def session(subject: Annotated[str, Depends(identity)]):
        return {"subject": subject}

    @app.get("/management/api/repositories", dependencies=[Depends(identity)])
    def repository_names():
        return {"repositories": sorted(repositories())}

    @app.get("/management/api/tasks", dependencies=[Depends(identity)])
    async def tasks(request: Request):
        if set(request.query_params) - {"repo", "status", "limit", "offset"}:
            raise HTTPException(400, "invalid-query")
        _, data = await gateway("GET", "/api/v1/tasks", params=list(request.query_params.multi_items()))
        return {"tasks": [task_summary(task) for task in data["tasks"]],
                "total": data["total"], "limit": data["limit"], "offset": data["offset"]}

    @app.get("/management/api/tasks/{task_id}", dependencies=[Depends(identity)])
    async def task(task_id: str):
        if not TASK_ID.fullmatch(task_id):
            raise HTTPException(400, "invalid-task-id")
        _, data = await gateway("GET", "/api/v1/tasks/" + task_id)
        summary = task_summary(data)
        result = data.get("result")
        pr_url = result.get("pr_url") if isinstance(result, dict) else None
        repo = data.get("repo")
        summary["pr_url"] = pr_url if (isinstance(pr_url, str) and isinstance(repo, str) and re.fullmatch(
            r"https://github\.com/" + re.escape(repo) + r"/pull/[1-9][0-9]*", pr_url, re.IGNORECASE)) else None
        return summary

    @app.post("/management/api/tasks", dependencies=[Depends(identity)])
    async def submit(request: Request):
        if request.headers.get("origin") != origin or request.headers.get("x-management-request") != "1":
            raise HTTPException(403, "same-origin-request-required")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "json-required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, "request-too-large")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "invalid-submission") from None
        if (not isinstance(payload, dict) or set(payload) != {"repo", "issue_number"}
                or not isinstance(payload["repo"], str) or not REPOSITORY.fullmatch(payload["repo"])
                or type(payload["issue_number"]) is not int or not 1 <= payload["issue_number"] <= 2147483647):
            raise HTTPException(400, "invalid-submission")
        allowed = repositories()
        repo = next((repo for repo in allowed if repo.lower() == payload["repo"].lower()), None)
        if repo is None:
            raise HTTPException(403, "repository-not-authorized")
        status, data = await gateway("POST", "/api/v1/tasks/spawn",
                                     payload={"repo": repo, "issue_number": payload["issue_number"]},
                                     repository_token=allowed[repo])
        return JSONResponse(task_summary(data), status_code=status)

    @app.get("/management/api/config", dependencies=[Depends(identity)])
    def config():
        try:
            runtime = json.loads(Path(settings["MANAGEMENT_RUNTIME_CONFIG_FILE"]).read_text())
            catalog = yaml.safe_load(runtime["catalog"])
            roles = catalog["roles"]
            agents = [{"id": role["id"], "role": name, "endpoint_alias": role["endpoint_alias"],
                       "max_files": role["max_files"], "tools": role["tools"], "availability": "unknown"}
                      for name, role in roles.items() if name in ("coder", "judge")]
            models = [{"role": role, "model": runtime[role + "_model"], "availability": "unknown",
                       "native_download": runtime[role + "_native_download"]} for role in ("coder", "judge")]
            return {"source": "Git/Helm", "version": runtime["version"],
                    "hash": settings.get("MANAGEMENT_CONFIG_HASH"), "agents": agents, "models": models}
        except (KeyError, OSError, ValueError, TypeError, yaml.YAMLError):
            raise HTTPException(503, "configuration-summary-unavailable") from None

    @app.get("/{path:path}", dependencies=[Depends(identity)])
    def frontend(path: str):
        if path.startswith(("management/api/", "api/")):
            raise HTTPException(404, "not-found")
        root = Path(settings.get("MANAGEMENT_STATIC_DIR", "ui/dist")).resolve()
        requested = (root / path).resolve()
        if not requested.is_relative_to(root):
            raise HTTPException(404, "not-found")
        asset = requested if requested.is_file() else root / "index.html"
        if not asset.is_file():
            raise HTTPException(503, "frontend-not-built")
        return FileResponse(asset)

    return app


app = create_app()