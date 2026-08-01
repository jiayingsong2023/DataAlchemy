"""Durable Kubernetes-job handles used by the single AgentRuntime."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from core.evidence import EvidenceObjectStore, ObjectNotFound, sha256
from storage.postgres import PostgresDatabase


@dataclass(frozen=True)
class JobObservation:
    state: str
    uid: str | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None


def _output_key(job: dict[str, Any]) -> str:
    prefix = f"runs/{job['run_id']}/jobs/{job['job_id']}/output"
    if job["input_key"].startswith("s3a://"):
        bucket = job["input_key"].removeprefix("s3a://").split("/", 1)[0]
        return f"s3a://{bucket}/{prefix}"
    return prefix


class JobBackend(Protocol):
    def submit(self, job: dict[str, Any]) -> JobObservation: ...

    def observe(self, job: dict[str, Any]) -> JobObservation: ...

    def cancel(self, job: dict[str, Any]) -> JobObservation: ...


class KubernetesJobBackend:
    """One hard-coded Job shape for H2 rough cleaning; no client-provided YAML."""

    def __init__(self, namespace: str | None = None, image: str | None = None):
        self.namespace = namespace or os.getenv("HARNESS_JOB_NAMESPACE", "data-alchemy")
        self.image = image or os.getenv("SPARK_IMAGE", "data-alchemy-harness:latest")

    @staticmethod
    def _api() -> Any:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        return client.BatchV1Api(), client

    def submit(self, job: dict[str, Any]) -> JobObservation:
        api, client = self._api()
        labels = {
            "app.kubernetes.io/managed-by": "dataalchemy-harness",
            "dataalchemy.io/run-id": job["run_id"],
            "dataalchemy.io/task-id": job["task_id"],
            "dataalchemy.io/step-id": job["step_id"],
            "dataalchemy.io/job-id": job["job_id"],
        }
        container = client.V1Container(
            name="spark-rough-clean",
            image=self.image,
            image_pull_policy=os.getenv("HARNESS_JOB_IMAGE_PULL_POLICY", "Never"),
            command=["python", "-m", "src.etl.main"],
            args=[
                "--input",
                job["input_key"],
                "--output",
                _output_key(job),
                "--result-manifest",
                job["result_key"],
                "--job-id",
                job["job_id"],
                "--input-sha256",
                job["input_sha256"],
            ],
            env=[
                client.V1EnvVar(name="HARNESS_RUN_ID", value=job["run_id"]),
                client.V1EnvVar(name="HARNESS_JOB_ID", value=job["job_id"]),
                client.V1EnvVar(
                    name="AWS_ACCESS_KEY_ID",
                    value=os.getenv(
                        "HARNESS_JOB_AWS_ACCESS_KEY_ID",
                        os.getenv("AWS_ACCESS_KEY_ID", "minioadmin"),
                    ),
                ),
                client.V1EnvVar(
                    name="AWS_SECRET_ACCESS_KEY",
                    value=os.getenv(
                        "HARNESS_JOB_AWS_SECRET_ACCESS_KEY",
                        os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin"),
                    ),
                ),
                client.V1EnvVar(
                    name="S3_ENDPOINT",
                    value=os.getenv(
                        "HARNESS_JOB_S3_ENDPOINT", os.getenv("S3_ENDPOINT", "http://minio:9000")
                    ),
                ),
                client.V1EnvVar(
                    name="S3_BUCKET", value=os.getenv("S3_BUCKET", "data-alchemy")
                ),
            ],
            security_context=client.V1SecurityContext(
                allow_privilege_escalation=False,
                capabilities=client.V1Capabilities(drop=["ALL"]),
                seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
            ),
        )
        body = client.V1Job(
            metadata=client.V1ObjectMeta(name=job["external_name"], labels=labels),
            spec=client.V1JobSpec(
                backoff_limit=0,
                ttl_seconds_after_finished=600,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=labels),
                    spec=client.V1PodSpec(
                        restart_policy="Never",
                        containers=[container],
                    ),
                ),
            ),
        )
        try:
            created = api.create_namespaced_job(self.namespace, body)
        except client.ApiException as error:
            if error.status != 409:
                raise
            created = api.read_namespaced_job(job["external_name"], self.namespace)
        return JobObservation("running", str(created.metadata.uid))

    def observe(self, job: dict[str, Any]) -> JobObservation:
        api, client = self._api()
        try:
            current = api.read_namespaced_job(job["external_name"], self.namespace)
        except client.ApiException as error:
            if error.status == 404:
                return JobObservation("orphaned", error_code="job_not_found")
            raise
        status = current.status
        if status.failed:
            return JobObservation("failed", str(current.metadata.uid), error_code="job_failed")
        if status.succeeded:
            return JobObservation(
                "succeeded", str(current.metadata.uid), error_code="job_result_missing"
            )
        return JobObservation("running", str(current.metadata.uid))

    def cancel(self, job: dict[str, Any]) -> JobObservation:
        api, client = self._api()
        try:
            api.delete_namespaced_job(
                job["external_name"], self.namespace, propagation_policy="Foreground"
            )
        except client.ApiException as error:
            if error.status != 404:
                raise
        return JobObservation("cancelled")


class JobService:
    def __init__(
        self,
        database_url: str,
        backend: JobBackend,
        result_store: EvidenceObjectStore | None = None,
    ):
        self.database = PostgresDatabase(database_url)
        self.backend = backend
        self.result_store = result_store

    @staticmethod
    def _name(task: dict[str, Any], step: dict[str, Any]) -> str:
        return f"da-{task['run_id'][:8]}-{step['step_id'][:8]}-a1"

    def request(
        self, task: dict[str, Any], step: dict[str, Any], identity: dict[str, str]
    ) -> dict[str, Any]:
        arguments = step["arguments"]
        input_key, input_sha256 = arguments["input_key"], arguments["input_sha256"]
        job_id = str(uuid.uuid4())
        result_key = f"runs/{task['run_id']}/jobs/{job_id}/result.json"
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=int(arguments.get("deadline_seconds", 3600))
        )
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_jobs (job_id, tenant_id, run_id, task_id, step_id, kind, backend, state, "
                    "external_name, input_key, input_sha256, result_key, deadline_at) "
                    "VALUES (%s, %s, %s, %s, %s, 'spark_rough_clean', 'kubernetes', 'requested', %s, %s, %s, %s, %s)",
                    (
                        job_id,
                        task["tenant_id"],
                        task["run_id"],
                        task["task_id"],
                        step["step_id"],
                        self._name(task, step),
                        input_key,
                        input_sha256,
                        result_key,
                        deadline,
                    ),
                )
                cursor.execute(
                    "INSERT INTO harness_outbox (outbox_id, tenant_id, run_id, task_id, step_id, job_id, kind, dedupe_key) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'submit_job', %s)",
                    (
                        str(uuid.uuid4()),
                        task["tenant_id"],
                        task["run_id"],
                        task["task_id"],
                        step["step_id"],
                        job_id,
                        f"job:{task['run_id']}:{step['step_id']}",
                    ),
                )
        return self.get(job_id, identity)

    def get(self, job_id: str, identity: dict[str, str]) -> dict[str, Any]:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM agent_jobs WHERE job_id = %s", (job_id,))
                row = cursor.fetchone()
        if row is None:
            raise PermissionError("Job not found")
        for key in ("job_id", "run_id", "task_id", "step_id"):
            row[key] = str(row[key])
        return row

    def request_cancel(self, task: dict[str, Any], identity: dict[str, str]) -> None:
        job = self.for_task(task, identity)
        if job is None:
            raise RuntimeError("Task job handle is missing")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_jobs SET state = 'cancel_requested' WHERE job_id = %s "
                    "AND state IN ('requested', 'submitting', 'running')",
                    (job["job_id"],),
                )
                cursor.execute(
                    "INSERT INTO harness_outbox "
                    "(outbox_id, tenant_id, run_id, task_id, step_id, job_id, kind, dedupe_key) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'cancel_job', %s) "
                    "ON CONFLICT (dedupe_key) DO NOTHING",
                    (
                        str(uuid.uuid4()),
                        task["tenant_id"],
                        task["run_id"],
                        task["task_id"],
                        job["step_id"],
                        job["job_id"],
                        f"cancel:{job['job_id']}",
                    ),
                )

    def for_task(self, task: dict[str, Any], identity: dict[str, str]) -> dict[str, Any] | None:
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT job_id FROM agent_jobs WHERE task_id = %s AND step_id = %s",
                    (task["task_id"], task["plan"][task["current_step"]]["step_id"]),
                )
                row = cursor.fetchone()
        return self.get(str(row["job_id"]), identity) if row else None

    def reconcile(self, job: dict[str, Any], identity: dict[str, str]) -> JobObservation:
        if job["state"] == "requested":
            observation = self.backend.submit(job)
        elif job["state"] == "cancel_requested":
            observation = self.backend.cancel(job)
        else:
            observation = self.backend.observe(job)
        if observation.state == "succeeded" and observation.result is None:
            observation = self._read_result(job, identity)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_jobs SET state = %s, external_uid = COALESCE(%s, external_uid), "
                    "last_observed_at = now(), error_code = %s, submitted_at = CASE WHEN %s = 'running' "
                    "THEN COALESCE(submitted_at, now()) ELSE submitted_at END, completed_at = CASE WHEN %s "
                    "IN ('succeeded', 'failed', 'cancelled', 'orphaned') THEN now() ELSE completed_at END "
                    "WHERE job_id = %s",
                    (
                        observation.state,
                        observation.uid,
                        observation.error_code,
                        observation.state,
                        observation.state,
                        job["job_id"],
                    ),
                )
        return observation

    def _read_result(self, job: dict[str, Any], identity: dict[str, str]) -> JobObservation:
        if self.result_store is None or not job.get("result_key"):
            return JobObservation("failed", error_code="job_result_missing")
        try:
            body = self.result_store.get(job["result_key"])
            payload = json.loads(body)
        except (ObjectNotFound, ValueError, json.JSONDecodeError):
            return JobObservation("failed", error_code="job_result_missing")
        if (
            payload.get("job_id") != job["job_id"]
            or payload.get("input_key") != job["input_key"]
            or payload.get("input_sha256") != job["input_sha256"]
            or not isinstance(payload.get("tool_result"), dict)
        ):
            return JobObservation("failed", error_code="job_result_invalid")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_jobs SET result_sha256 = %s WHERE job_id = %s",
                    (sha256(body), job["job_id"]),
                )
        return JobObservation("succeeded", result=payload["tool_result"])
