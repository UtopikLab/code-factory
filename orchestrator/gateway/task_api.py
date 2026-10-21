"""Task intake, callback, retry, and status operations for the gateway."""


class TaskAPI:
    def __init__(self, handler, gateway):
        self.handler = handler
        self.gateway = gateway

    def lookup(self, db):
        gateway = self.gateway
        parsed = gateway.urlsplit(self.handler.path)
        task_id = parsed.path.removeprefix("/api/v1/tasks/").removesuffix("/sync").removesuffix("/retry")
        query = gateway.parse_qs(parsed.query)
        if not gateway.re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", task_id):
            raise ValueError("invalid-task-id")
        repo = query.get("repo", [None])[0]
        task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None and task_id.isdigit() and repo:
            task = db.execute("SELECT * FROM tasks WHERE lower(repo)=lower(?) AND issue=?", (repo, int(task_id))).fetchone()
        if task and repo and repo.lower() != (task["repo"] or "").lower():
            raise PermissionError("repository-mismatch")
        return task

    def get_task(self):
        gateway, handler = self.gateway, self.handler
        db = gateway._task_db()
        try:
            task = self.lookup(db)
            if task is None:
                handler._json(404, {"error": "task-not-found"})
                return
            handler._json(200, {
                "task_id": task["task_id"], "repo": task["repo"], "issue_number": task["issue"],
                "status": task["status"], "result": gateway.json.loads(task["result"]) if task["result"] else None,
                "lifecycle": task["lifecycle"], "credential_cleanup": task["credential_cleanup"],
                "retry_of": task["retry_of"], "attempts": task["attempts"],
                "config_version": task["config_version"], "config_hash": task["config_hash"],
                "retry_task_id": next((row[0] for row in db.execute(
                    "SELECT task_id FROM tasks WHERE retry_of=?", (task["task_id"],))), None),
                "job": gateway.json.loads(task["job"]) if task["job"] else None,
            })
        except PermissionError:
            handler._json(403, {"error": "repository-mismatch"})
        except ValueError:
            handler._json(400, {"error": "invalid-task-id"})
        finally:
            db.close()

    def retry(self, payload):
        gateway, handler = self.gateway, self.handler
        gateway._reject_credentials(payload, handler.headers.get("X-Repository-Token", ""))
        if set(payload) != {"retry_key"} or not isinstance(payload["retry_key"], str) or not gateway.re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", payload["retry_key"]
        ):
            raise ValueError("invalid-retry-request")
        db = gateway._task_db()
        try:
            task = self.lookup(db)
            if task is None:
                handler._json(404, {"error": "task-not-found"})
                return
            original = gateway.json.loads(task["payload"])
        finally:
            db.close()
        original.pop("task_id", None)
        self.spawn(original, retry_of=task["task_id"], retry_key=payload["retry_key"])

    def spawn(self, payload, retry_of=None, retry_key=None):
        gateway, handler = self.gateway, self.handler
        token = handler.headers.get("X-Repository-Token", "")
        gateway._reject_credentials(payload, token)
        repo, issue = gateway._identity(payload)
        if "worker_id" in payload:
            raise ValueError("gateway-assignment-required")
        if gateway._is_external_workflow_payload(payload) and issue is None:
            raise ValueError("issue-required")
        if "task_id" in payload:
            gateway._validate_assigned_task_id(payload["task_id"])
            if issue is not None and str(payload["task_id"]) != str(issue):
                raise ValueError("issue-mismatch")
        if not handler._transport_allowed():
            raise gateway.CredentialError("transport-not-allowed")
        gateway._validate_repository(repo, issue, token)
        task_id = "issue-" + gateway.hashlib.sha256(f"{repo.lower()}#{issue}".encode()).hexdigest()[:32] if issue else gateway.uuid.uuid4().hex
        if retry_of:
            task_id = "issue-" + gateway.hashlib.sha256(f"retry:{retry_of}:{retry_key}".encode()).hexdigest()[:32]
        assigned = gateway.copy.deepcopy(payload)
        assigned["task_id"] = task_id
        if repo:
            assigned["repo"] = repo
        db = gateway._task_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if old:
                db.commit()
                code = 409 if old["status"] == "failed" else 202
                handler._json(code, {"task_id": task_id, "repo": repo, "status": old["status"], "replayed": True})
                return
            if retry_of:
                parent = db.execute("SELECT * FROM tasks WHERE task_id=?", (retry_of,)).fetchone()
                child = db.execute("SELECT task_id FROM tasks WHERE retry_of=?", (retry_of,)).fetchone()
                if child:
                    db.commit()
                    handler._json(409, {"error": "retry-already-created", "task_id": child["task_id"]})
                    return
                if not parent or parent["status"] != "failed" or not parent["result"]:
                    db.commit()
                    handler._json(409, {"error": "retry-requires-failed-task"})
                    return
                settled = {"synced", "credential-missing", "credential-expired", "credential-denied",
                           "credential-binding-mismatch", "credential-invalid"}
                if parent["lifecycle"] not in settled or parent["credential_cleanup"] != "deleted":
                    db.commit()
                    handler._json(409, {"error": "retry-requires-lifecycle-sync"})
                    return
                active = db.execute("SELECT task_id FROM tasks WHERE lower(repo)=lower(?) AND issue=? AND status IN ('queued','running')",
                                    (repo, issue)).fetchone()
                if active:
                    db.commit()
                    handler._json(409, {"error": "issue-task-active", "task_id": active["task_id"]})
                    return
            callback = gateway.secrets.token_urlsafe(32)
            credential_ref = "task-credential-" + task_id
            config_version = gateway.os.environ.get("AGENT_CONFIG_VERSION", "1")
            config_hash = gateway.os.environ.get("AGENT_CONFIG_HASH") or gateway.hashlib.sha256(config_version.encode()).hexdigest()
            db.execute("INSERT INTO tasks(task_id,repo,issue,status,payload,credential_ref,callback_hash,retry_of,retry_key,config_version,config_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (task_id, repo, issue, "queued", gateway.json.dumps(assigned), credential_ref, gateway.hashlib.sha256(callback.encode()).hexdigest(),
                        retry_of, retry_key, config_version, config_hash))
            db.commit()
            try:
                credential_uid = gateway._create_task_secret(task_id, repo, token, callback)
                db.execute("UPDATE tasks SET credential_uid=? WHERE task_id=?", (credential_uid, task_id))
            except gateway.KubernetesError:
                failure = gateway.json.dumps({"task_id": task_id, "repo": repo, "issue_number": issue,
                                              "status": "failed", "error": "credential-create-failed"})
                db.execute("UPDATE tasks SET status='failed', result=? WHERE task_id=?", (failure, task_id))
                gateway._retry_lifecycle(task_id)
                handler._json(503, {"task_id": task_id, "error": "credential-create-failed"})
                return
            handler._json(202, {"task_id": task_id, "repo": repo, "status": "queued"})
        finally:
            db.close()

    def resync(self, payload):
        gateway, handler = self.gateway, self.handler
        if payload:
            raise ValueError("sync-body-must-be-empty")
        db = gateway._task_db()
        try:
            task = self.lookup(db)
            if task is None:
                handler._json(404, {"error": "task-not-found"})
                return
            if task["status"] not in ("completed", "failed") or not task["result"]:
                handler._json(409, {"error": "task-not-terminal"})
                return
        finally:
            db.close()
        lifecycle = gateway._retry_lifecycle(task["task_id"])
        handler._json(200, {"task_id": task["task_id"], "status": task["status"], "lifecycle": lifecycle})

    def result(self, payload):
        gateway, handler = self.gateway, self.handler
        gateway._reject_credentials(payload, handler.headers.get("X-Task-Token", ""))
        repo, issue = gateway._identity(payload)
        db = gateway._task_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = self.lookup(db)
            if task is None:
                db.commit()
                handler._json(404, {"error": "task-not-found"})
                return
            callback = handler.headers.get("X-Task-Token", "")
            if not callback or not task["callback_hash"] or not gateway.hmac.compare_digest(gateway.hashlib.sha256(callback.encode()).hexdigest(), task["callback_hash"]):
                raise PermissionError("task-callback-denied")
            legacy_id = gateway.urlsplit(handler.path).path.rsplit("/", 1)[1].isdigit() and repo is not None
            if str(payload.get("task_id")) != (str(task["issue"]) if legacy_id else task["task_id"]):
                raise ValueError("task-id-mismatch")
            if (repo or "").lower() != (task["repo"] or "").lower() or issue != task["issue"]:
                raise ValueError("result-identity-mismatch")
            if payload.get("status") not in ("candidate", "candidate_failed"):
                raise ValueError("candidate-status-required")
            if task["status"] in ("completed", "failed"):
                db.commit()
                handler._json(409, {"error": "task-already-terminal"})
                return
            if payload["status"] == "candidate" and (payload.get("error") or not payload.get("patch")):
                raise ValueError("empty-candidate")
            serialized = gateway.json.dumps(payload, sort_keys=True)
            if task["result"] and gateway.json.loads(task["result"]) != payload:
                db.commit()
                handler._json(409, {"error": "terminal-result-conflict"})
                return
            db.execute("UPDATE tasks SET result=? WHERE task_id=?", (serialized, task["task_id"]))
            db.commit()
            handler._json(200, {"task_id": task["task_id"], "status": payload["status"]})
        finally:
            db.close()

    def supervisor_result(self, payload):
        gateway, handler = self.gateway, self.handler
        if not handler._supervisor_authorized():
            handler._json(401, {"error": "supervisor-unauthorized"})
            return
        task_id = gateway.urlsplit(handler.path).path.removeprefix("/api/v1/supervisor/tasks/")
        if not gateway.re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", task_id):
            raise ValueError("invalid-task-id")
        if payload.get("task_id") != task_id or payload.get("status") not in ("completed", "failed"):
            raise ValueError("invalid-supervisor-result")
        if payload["status"] == "completed" and not gateway.re.fullmatch(
                r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*",
                payload.get("pr_url", ""), gateway.re.IGNORECASE):
            raise ValueError("human-review-pr-required")
        gateway._reject_credentials(payload)
        db = gateway._task_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                db.commit()
                handler._json(404, {"error": "task-not-found"})
                return
            result = gateway.json.dumps(payload, sort_keys=True)
            if task["status"] in ("completed", "failed") and task["result"] != result:
                db.commit()
                handler._json(409, {"error": "terminal-result-conflict"})
                return
            db.execute("UPDATE tasks SET status=?, result=?, claim_owner=NULL, claimed_at=NULL WHERE task_id=?",
                       (payload["status"], result, task_id))
            db.commit()
            lifecycle = gateway._retry_lifecycle(task_id)
            handler._json(200, {"task_id": task_id, "status": payload["status"], "lifecycle": lifecycle})
        finally:
            db.close()