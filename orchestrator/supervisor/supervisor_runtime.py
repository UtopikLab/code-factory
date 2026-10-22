"""Resident durable task polling and supervisor workflow coordination."""

import asyncio
import json
import time
import uuid


class SupervisorLoop:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.graph = supervisor.create_supervisor_graph()
        self.logger = supervisor.logger

    def _record_task_metrics(self, state, start_time):
        supervisor = self.supervisor
        duration = max(0.0, asyncio.get_running_loop().time() - start_time)
        supervisor.DURATION.observe(duration)
        status = state.get("status", "unknown")
        supervisor.TASKS.labels(status=status).inc()
        supervisor.ITERATIONS.inc(state.get("iteration", 1))
        supervisor.ACTIVE_TASKS.set(0)
        if state.get("best_output"):
            supervisor.BEST_SCORE.set(state["best_output"].get("score", 0.0))
        supervisor.SUPERVISOR_STATE.set(self._state_to_value(status))
        supervisor.CURRENT_ITERATION.set(state.get("iteration", 1))
        supervisor.logger.info(
            f"[{state['task_id']}] Metrics: status={status} duration={duration:.2f}s "
            f"best_score={state['best_output']['score'] if state.get('best_output') else 'n/a'}")

    @staticmethod
    def _state_to_value(status):
        mapping = {"idle": 0.0, "planning": 1.0, "executing": 2.0, "evaluating": 3.0,
                   "complete": 4.0, "failed": -1.0, "error": -2.0}
        return mapping.get(status, -9.0)

    async def run_task(self, task):
        supervisor = self.supervisor
        start_time = asyncio.get_running_loop().time()
        supervisor.ACTIVE_TASKS.set(1)
        supervisor.SUPERVISOR_STATE.set(self._state_to_value("planning"))
        result = await self.graph.ainvoke(task)
        self._record_task_metrics(result, start_time)
        return result

    async def process_next_durable_task(self):
        supervisor = self.supervisor
        owner = supervisor.os.environ.get("SUPERVISOR_INSTANCE_ID", str(uuid.uuid4()))
        db = supervisor.task_database()
        task = supervisor.claim_queued_task(db, owner)
        if task is None:
            db.close()
            return False
        task_id = task["task_id"]
        runtime = None
        try:
            catalog, runtime = supervisor._load_pinned_catalog(task)
            if task["attempts"] >= supervisor.MAX_TOTAL_ATTEMPTS and not task["result"]:
                supervisor._complete_via_gateway(task_id, "failed", runtime, error="max-total-attempts-exhausted")
                return True
            if not task["plan"]:
                plan = await supervisor._plan_task(task, catalog, runtime)
                db.execute("UPDATE tasks SET plan=? WHERE task_id=? AND claim_owner=?", (plan, task_id, owner))
                task = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task["result"]:
                candidate = json.loads(task["result"])
            else:
                job_name = supervisor._dispatch_task(task, runtime)
                db.execute("UPDATE tasks SET job=? WHERE task_id=? AND claim_owner=?",
                           (json.dumps({"name": job_name}), task_id, owner))
                deadline = time.monotonic() + int(supervisor.os.environ.get("TASK_RESULT_TIMEOUT_SECONDS", "1800"))
                candidate = None
                while time.monotonic() < deadline:
                    current = db.execute("SELECT result, claim_owner FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                    if current is None or current["claim_owner"] != owner:
                        raise RuntimeError("task-claim-lost")
                    if current["result"]:
                        candidate = json.loads(current["result"])
                        break
                    await asyncio.sleep(float(supervisor.os.environ.get("TASK_POLL_SECONDS", "5")))
                if candidate is None:
                    raise RuntimeError("worker-result-timeout")
            if candidate.get("status") != "candidate":
                raise RuntimeError(candidate.get("error", "worker-candidate-failed"))
            candidate = supervisor._verified_candidate_patch(task, candidate)
            score, review = await supervisor._judge_candidate(task, candidate, runtime)
            threshold = float(runtime["threshold"])
            if not 0 <= threshold <= 1:
                raise RuntimeError("acceptance-threshold-invalid")
            if score < threshold:
                if task["attempts"] >= supervisor.MAX_TOTAL_ATTEMPTS:
                    supervisor._complete_via_gateway(task_id, "failed", runtime,
                                                     error="judge-rejected-after-eight-attempts", score=score, review=review)
                else:
                    db.execute("UPDATE tasks SET status='queued', result=NULL, claim_owner=NULL, claimed_at=NULL WHERE task_id=? AND claim_owner=?",
                               (task_id, owner))
                return True
            if not supervisor.os.environ.get("PR_GITHUB_TOKEN") and json.loads(task["payload"]).get("repository_auth") != "github-app":
                raise RuntimeError("pr-publication-credential-required")
            current = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            pr_url = supervisor._publish_candidate(current, candidate, runtime)
            supervisor._complete_via_gateway(task_id, "completed", runtime, pr_url=pr_url, score=score, review=review,
                                             config_version=task["config_version"], config_hash=task["config_hash"])
        except Exception as error:
            supervisor.logger.exception("durable task failed task=%s", task_id)
            terminal_errors = {"pr-policy-required", "pr-base-branch-not-allowed", "pr-reviewer-policy-invalid",
                               "pr-publication-credential-required", "judge-credential-required",
                               "planner-credential-required", "planner-output-invalid",
                               "candidate-publish-data-missing", "candidate-edit-policy-violation",
                               "task-config-pin-invalid", "task-catalog-hash-mismatch",
                               "task-catalog-schema-invalid", "task-catalog-version-unsupported", "task-catalog-role-set-invalid",
                               "task-catalog-role-invalid", "task-catalog-capability-boundary-invalid"}
            reason = str(error).split(":", 1)[0]
            if task["attempts"] >= supervisor.MAX_TOTAL_ATTEMPTS or reason in terminal_errors:
                try:
                    supervisor._complete_via_gateway(task_id, "failed", runtime, error=str(error)[:1000])
                except Exception:
                    supervisor.logger.exception("could not persist terminal failure task=%s", task_id)
            else:
                db.execute("UPDATE tasks SET status='queued', result=NULL, claim_owner=NULL, claimed_at=NULL WHERE task_id=? AND claim_owner=?",
                           (task_id, owner))
        finally:
            db.close()
        return True

    async def run_forever(self):
        supervisor = self.supervisor
        interval_seconds = int(supervisor.os.getenv("SUPERVISOR_IDLE_SLEEP_SECONDS", "15"))
        supervisor.SUPERVISOR_STATE.set(self._state_to_value("idle"))
        supervisor.logger.info("Supervisor durable task polling started.")
        while True:
            try:
                did_work = await self.process_next_durable_task()
            except Exception:
                supervisor.logger.exception("durable task poll failed")
                supervisor.SUPERVISOR_STATE.set(self._state_to_value("idle"))
                did_work = False
            if not did_work:
                await asyncio.sleep(interval_seconds)