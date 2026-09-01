"""Run the guarded PDF cycle through one fixed entrypoint and two resumable stages."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "src"))
from harness.receipts import write_receipt  # noqa: E402


class CycleError(RuntimeError):
    pass


class Api:
    def __init__(self, base_url: str, token: str | None = None, host: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.host = host

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        form: dict[str, str] | None = None,
        file: tuple[str, bytes, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.host:
            headers["Host"] = self.host
        payload: bytes | None = None
        if file:
            name, content, content_type = file
            boundary = f"----dataalchemy-{uuid.uuid4().hex}"
            fields = body or {}
            parts = []
            for key, value in fields.items():
                parts.append(
                    (
                        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                        f"{value}\r\n"
                    ).encode()
                )
            file_header = (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{name}"\r\n'
            )
            parts.append(
                file_header.encode()
                + f"Content-Type: {content_type}\r\n\r\n".encode()
                + content
                + b"\r\n"
            )
            parts.append(f"--{boundary}--\r\n".encode())
            payload = b"".join(parts)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif form is not None:
            payload = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=payload, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise CycleError(f"http_{error.code}:{path}:{detail[:500]}") from error
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise CycleError(f"invalid_json_response:{path}") from error

    def login(self, username: str, password: str) -> str:
        result = self.request(
            "POST", "/api/auth/login", form={"username": username, "password": password}
        )
        token = result.get("access_token")
        if not token:
            raise CycleError("login_token_missing")
        self.token = token
        return token


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", shlex.join(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def reset_cluster(cluster: str, confirmation: str | None) -> None:
    if cluster != "dataalchemy-gpu" or confirmation != cluster:
        raise CycleError("cluster_reset_requires_exact_confirmation:dataalchemy-gpu")
    subprocess.run(["k3d", "cluster", "delete", cluster], cwd=ROOT, check=False)


def deploy(cluster: str) -> None:
    env = os.environ.copy()
    env.update({"K3D_CLUSTER_NAME": cluster, "K3D_GPU_ENABLED": "true"})
    run(["bash", "scripts/setup/setup_k3d.sh"], env=env)
    web = os.getenv(
        "DATAALCHEMY_WEB_IMAGE",
        os.getenv("DATAALCHEMY_CORE_IMAGE", "data-alchemy:web-local"),
    )
    harness = os.getenv("DATAALCHEMY_HARNESS_IMAGE", "data-alchemy:h5-canonical-local")
    etl = os.getenv("DATAALCHEMY_ETL_IMAGE", "data-alchemy:etl-local")
    operator = os.getenv("DATAALCHEMY_OPERATOR_IMAGE", "dataalchemy-operator:h5-local")
    minio = os.getenv("DATAALCHEMY_MINIO_IMAGE", "minio/minio:RELEASE.2025-04-22T22-12-26Z")
    if not _image_exists("minio/minio:latest"):
        if not _image_exists(minio):
            raise CycleError(f"minio_image_missing:{minio}")
        run(["docker", "tag", minio, "minio/minio:latest"])
    if not _image_exists("redis:7.0-alpine"):
        raise CycleError("redis_image_missing:redis:7.0-alpine")
    if not _image_exists("pgvector/pgvector:pg16"):
        raise CycleError("postgres_image_missing:pgvector/pgvector:pg16")
    for image in (web, harness, etl):
        if not _image_exists(image):
            raise CycleError(f"application_image_missing:{image}")
    if not _image_exists(operator):
        run(["docker", "build", "-t", operator, "deploy/operator/"])
    run(
        [
            "k3d",
            "image",
            "import",
            web,
            harness,
            etl,
            operator,
            "pgvector/pgvector:pg16",
            "minio/minio:latest",
            "redis:7.0-alpine",
            "-c",
            cluster,
        ]
    )
    values = [
        "--set",
        f"images.core={web}",
        "--set",
        f"images.harnessJob={harness}",
        "--set",
        f"images.etl={etl}",
        "--set",
        f"images.operator={operator}",
        "--set",
        "images.pullPolicy=Never",
        "--set",
        "config.harnessJobGpuEnabled=true",
        "--set",
        "config.harnessJobGpuPrivileged=true",
        "--set-string",
        f"config.h5LoraMode={os.getenv('H5_LORA_MODE', 'disabled')}",
        "--set-string",
        f"config.modelReleaseTenantId={os.getenv('MODEL_RELEASE_TENANT_ID', '')}",
        "--set",
        "postgresql.enabled=true",
        "--set-string",
        f"credentials.authSecretKey={secrets.token_hex(32)}",
        "--set-string",
        f"credentials.postgresPassword={secrets.token_hex(24)}",
        "--set-string",
        f"credentials.postgresAppPassword={secrets.token_hex(24)}",
        "--set-string",
        f"credentials.postgresVerifierPassword={secrets.token_hex(24)}",
    ]
    run(
        [
            "helm",
            "upgrade",
            "--install",
            "data-alchemy",
            "deploy/charts/data-alchemy",
            "--namespace",
            "data-alchemy",
            "--create-namespace",
            "--wait",
            "--timeout",
            "15m",
            *values,
        ]
    )
    run(["bash", "scripts/setup/verify_gpu.sh", "data-alchemy"])


def _image_exists(name: str) -> bool:
    return (
        subprocess.run(
            ["docker", "image", "inspect", name],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def wait_run(api: Api, run_id: str, *, auto_approve: bool, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = api.request("GET", f"/api/runs/{run_id}")
        task = result.get("task", {})
        state = task.get("state")
        if state == "waiting_approval":
            if not auto_approve:
                return {
                    **result,
                    "state": "waiting_approval",
                    "next_action": "approve the document task, then rerun with --resume",
                }
            api.request(
                "POST",
                f"/api/tasks/{task['task_id']}/approval",
                {"approved": True, "expected_version": task.get("version")},
            )
        elif state == "waiting_job":
            # FastAPI's TaskControlRequest accepts an empty JSON object; an
            # omitted body is a 422 before the durable job can be reconciled.
            api.request(
                "POST",
                f"/api/runs/{run_id}/reconcile",
                {"expected_version": task.get("version")},
            )
        elif state in {"succeeded", "failed", "cancelled", "aborted"}:
            if state != "succeeded":
                raise CycleError(f"document_run_failed:{state}:{task.get('finish_reason')}")
            return result
        time.sleep(3)
    raise CycleError("document_run_timeout")


def _credentials(environment: str) -> tuple[str, str]:
    username = os.getenv("DATAALCHEMY_USERNAME")
    password = os.getenv("DATAALCHEMY_PASSWORD")
    if environment == "production" and (not username or not password):
        raise CycleError("production_credentials_required")
    return username or "admin", password or "admin123"


def run_webui_stage(args: argparse.Namespace) -> int:
    if args.auto_approve and args.environment != "engineering":
        raise CycleError("auto_approve_requires_engineering_environment")
    if args.resume:
        if not args.run_id:
            raise CycleError("webui_resume_requires_run_id")
        username, password = _credentials(args.environment)
        api = Api(args.base_url, host=args.host_header)
        api.login(username, password)
        result = wait_run(
            api,
            args.run_id,
            auto_approve=args.auto_approve or args.allow_auto_approve,
            timeout=args.timeout,
        )
        receipt = {
            "state": result.get("state", "passed"),
            "stage": "webui",
            "run_id": args.run_id,
            "next_action": (
                "continue in WebUI"
                if result.get("state") == "succeeded"
                else result.get("next_action")
            ),
            "document_run": result,
        }
        receipt_path = write_receipt(ROOT, args.run_id, receipt)
        receipt["receipt_path"] = str(receipt_path)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    if not args.pdf or not args.pdf.is_file() or args.pdf.suffix.lower() != ".pdf":
        raise CycleError("pdf_file_missing_or_not_pdf")
    if args.reset:
        reset_cluster(args.cluster, args.confirm_cluster_reset)
    if args.deploy:
        deploy(args.cluster)
    username, password = _credentials(args.environment)
    api = Api(args.base_url, host=args.host_header)
    api.login(username, password)
    probe_question = args.probe_question or (
        args.question[0] if args.question else "请概括本文档的主要内容"
    )
    pilot = api.request(
        "POST",
        "/api/pilot-runs/document",
        {"question": probe_question, "expected_phrase": args.expected_phrase, "acl": ""},
        file=(args.pdf.name, args.pdf.read_bytes(), "application/pdf"),
    )
    run_id = pilot["run_id"]
    result = wait_run(
        api,
        run_id,
        auto_approve=args.auto_approve or args.allow_auto_approve,
        timeout=args.timeout,
    )
    if result.get("state") == "waiting_approval":
        receipt = {
            "state": "waiting_approval",
            "stage": "webui",
            "run_id": run_id,
            "next_action": result["next_action"],
        }
        receipt_path = write_receipt(ROOT, run_id, receipt)
        receipt["receipt_path"] = str(receipt_path)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    probe = api.request("POST", "/api/chat", {"query": probe_question, "run_id": run_id})
    if not probe.get("citations"):
        raise CycleError("rag_citations_missing")
    receipt = {
        "state": "passed",
        "stage": "webui",
        "run_id": run_id,
        "webui_url": args.base_url,
        "probe_question": probe_question,
        "rag": {"citations": probe["citations"], "feedback_id": probe.get("feedback_id")},
        "memory": {"state": "waiting_for_conversation"},
        "next_action": "continue conversation in WebUI; review feedback before --stage h5",
        "document_run": result,
    }
    receipt_path = write_receipt(ROOT, run_id, receipt)
    receipt["receipt_path"] = str(receipt_path)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, default=str))
    return 0


def run_h5_stage(args: argparse.Namespace) -> int:  # noqa: C901 - one fixed gate path
    if not args.run_id or (not args.suite and not args.resume):
        raise CycleError("h5_requires_run_id_and_suite_or_resume")
    if args.h5_command:
        raise CycleError("arbitrary_h5_command_disabled_use_fixed_orchestrator")
    if args.auto_approve and args.environment != "engineering":
        raise CycleError("auto_approve_requires_engineering_environment")
    username, password = _credentials(args.environment)
    api = Api(args.base_url, host=args.host_header)
    api.login(username, password)
    tenant_id = api.request("GET", "/api/auth/me")["tenant_id"]
    annotation_ids = list(args.annotation_id)
    command = [
        sys.executable,
        str(ROOT / "scripts" / "run_h5_pdf_cycle.py"),
        "--run-id",
        args.run_id,
        "--tenant-id",
        tenant_id,
        "--environment",
        args.environment,
    ]
    if args.suite:
        command.extend(["--suite", str(args.suite)])
    for annotation_id in annotation_ids:
        command.extend(["--annotation-id", annotation_id])
    if args.attempt_id:
        command.extend(["--attempt-id", args.attempt_id])
    if args.resume:
        command.append("--resume")
    if args.allow_auto_approve:
        command.append("--allow-auto-approve")
    if args.canary_observation:
        command.extend(["--canary-observation", str(args.canary_observation)])
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    if completed.returncode:
        raise CycleError(f"h5_orchestrator_failed:{completed.returncode}")
    receipt = None
    for line in reversed(completed.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and (candidate.get("run_id") or candidate.get("status")):
            receipt = candidate
            break
    if receipt is None:
        raise CycleError("h5_receipt_missing")
    if receipt.get("state") in {"waiting_input", "waiting_approval", "already_running"}:
        return 0
    if receipt.get("status") != "passed":
        raise CycleError(f"h5_receipt_not_passed:{receipt.get('status', receipt.get('state'))}")
    if args.skip_webui_verification:
        return 0
    release_id = receipt.get("release_id")
    adapter_id = receipt.get("adapter_id")
    if not release_id or not adapter_id:
        raise CycleError("h5_receipt_release_or_adapter_missing")
    reload_result = api.request(
        "POST",
        "/api/models/reload",
        {
            "release_id": release_id,
            "expected_adapter_id": adapter_id,
            "expected_artifact_sha256": receipt.get("adapter_artifact_sha256"),
        },
    )
    if reload_result.get("status") not in {"succeeded", "already_current"}:
        raise CycleError(f"model_reload_failed:{reload_result.get('status')}")
    model_execution = reload_result.get("model_execution", {})
    if (
        model_execution.get("release_id") != release_id
        or model_execution.get("adapter_id") != adapter_id
    ):
        raise CycleError("model_reload_evidence_mismatch")
    question = args.probe_question or "请概括本文档的主要内容"
    chat = api.request("POST", "/api/chat", {"query": question, "run_id": args.run_id})
    if not chat.get("citations"):
        raise CycleError("adapter_webui_citations_missing")
    execution = chat.get("model_execution", {})
    if execution.get("release_id") != release_id or execution.get("adapter_id") != adapter_id:
        raise CycleError("adapter_webui_execution_mismatch")
    receipt.update(
        {
            "stage": "h5",
            "model_reload": {
                "status": reload_result["status"],
                "release_id": release_id,
                "adapter_id": adapter_id,
                "model_execution": model_execution,
            },
            "webui_verification": {
                "status": "passed",
                "question": question,
                "citations": chat["citations"],
                "model_execution": execution,
            },
        }
    )
    receipt_path = write_receipt(ROOT, args.run_id, receipt)
    receipt["receipt_path"] = str(receipt_path)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("webui", "h5"), default="webui")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument(
        "--question",
        action="append",
        default=[],
        help="Optional probe question; repeat for legacy callers",
    )
    parser.add_argument("--probe-question")
    parser.add_argument("--expected-phrase", default="")
    parser.add_argument(
        "--base-url", default=os.getenv("DATAALCHEMY_WEBUI_URL", "http://data-alchemy.test")
    )
    parser.add_argument(
        "--host-header", default=os.getenv("DATAALCHEMY_HOST_HEADER", "data-alchemy.test")
    )
    parser.add_argument("--cluster", default="dataalchemy-gpu")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--confirm-cluster-reset")
    parser.add_argument("--deploy", action="store_true")
    parser.add_argument(
        "--environment", choices=("production", "engineering"), default="production"
    )
    parser.add_argument("--allow-auto-approve", action="store_true")
    parser.add_argument("--auto-approve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--run-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--suite", type=Path)
    parser.add_argument("--annotation-id", action="append", default=[])
    parser.add_argument("--canary-observation", type=Path)
    parser.add_argument("--skip-webui-verification", action="store_true")
    parser.add_argument("--h5-command", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.auto_approve:
        args.allow_auto_approve = True
    if args.stage == "webui":
        return run_webui_stage(args)
    return run_h5_stage(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CycleError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error
