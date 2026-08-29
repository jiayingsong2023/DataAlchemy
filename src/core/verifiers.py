"""Deterministic, read-only verifier contracts for the agent harness."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Callable

from src.harness.deployment import DeploymentBinding, validate_shadow_output
from storage.postgres import PostgresDatabase
from utils.s3_utils import S3Utils


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class VerificationResult:
    status: str
    summary: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


@dataclass(frozen=True)
class VerifierSpec:
    name: str
    version: int
    handler: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any], "ReadOnlyServices"], VerificationResult
    ]
    timeout_seconds: float = 30.0
    max_attempts: int = 2

    @property
    def contract_digest(self) -> str:
        return _digest(
            {
                "name": self.name,
                "version": self.version,
                "timeout_seconds": self.timeout_seconds,
                "max_attempts": self.max_attempts,
            }
        )


class ReadOnlyServices:
    """Verifier-only PostgreSQL reads. The transaction rejects writes server-side."""

    def __init__(self, database_url: str, identity: dict[str, str]):
        self.database = PostgresDatabase(database_url)
        self.identity = identity

    def documents(self, document_ids: list[str]) -> list[dict[str, Any]]:
        if not document_ids:
            return []
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT d.document_id, d.source_uri, d.content_hash, d.version, d.status, d.metadata_json, "
                    "count(c.chunk_id) AS chunk_count "
                    "FROM documents d LEFT JOIN document_chunks c ON c.document_id = d.document_id "
                    "WHERE d.document_id = ANY(%s) GROUP BY d.document_id",
                    (document_ids,),
                )
                return [
                    {
                        **row,
                        "document_id": str(row["document_id"]),
                        "metadata": row.pop("metadata_json"),
                    }
                    for row in cursor.fetchall()
                ]

    @staticmethod
    def _object_parts(key: str) -> tuple[S3Utils, str]:
        normalized = key.replace("s3a://", "s3://", 1)
        if normalized.startswith("s3://"):
            bucket, _, object_key = normalized.removeprefix("s3://").partition("/")
            return S3Utils(bucket=bucket), object_key
        return S3Utils(), normalized

    def object_body(self, key: str) -> bytes | None:
        store, object_key = self._object_parts(key)
        return store.get_object_body(object_key)

    def object_json(self, key: str) -> Any:
        body = self.object_body(key)
        if body is None:
            return None
        return json.loads(body)

    def object_records(self, prefix: str) -> list[dict[str, Any]]:
        store, object_prefix = self._object_parts(prefix)
        records: list[dict[str, Any]] = []
        for item in sorted(
            store.list_objects(object_prefix.rstrip("/") + "/"), key=lambda value: value["Key"]
        ):
            if not item["Key"].endswith((".json", ".jsonl")):
                continue
            body = store.get_object_body(item["Key"])
            if body is None:
                continue
            records.extend(
                json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()
            )
        return records

    def matching_chunks(self, document_id: str, query: str) -> int:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) AS count FROM document_chunks "
                    "WHERE document_id = %s AND fts @@ plainto_tsquery('simple', %s)",
                    (document_id, query),
                )
                return int(cursor.fetchone()["count"])

    def chunks(self, document_ids: list[str]) -> list[dict[str, Any]]:
        if not document_ids:
            return []
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT chunk_id, document_id, ordinal, metadata_json FROM document_chunks "
                    "WHERE document_id = ANY(%s) ORDER BY document_id, ordinal",
                    (document_ids,),
                )
                return [
                    {
                        **row,
                        "chunk_id": str(row["chunk_id"]),
                        "document_id": str(row["document_id"]),
                        "metadata": row.pop("metadata_json"),
                    }
                    for row in cursor.fetchall()
                ]

    def memory(self, memory_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT memory_id, status, source_event_id, valid_until, content_hash FROM memories "
                    "WHERE memory_id = %s",
                    (memory_id,),
                )
                return cursor.fetchone()

    def context_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT snapshot_id, tenant_id, identity_digest, pack_refs, budget_json, envelope_sha256 "
                    "FROM context_snapshots WHERE snapshot_id = %s",
                    (snapshot_id,),
                )
                return cursor.fetchone()

    def context_checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT checkpoint_id, session_id, tenant_id, source_sequence_start, source_sequence_end, source_digest, "
                    "summary, handoff_json, status FROM context_checkpoints WHERE checkpoint_id = %s",
                    (checkpoint_id,),
                )
                return cursor.fetchone()

    def conversation_events(
        self, session_id: str, sequence_start: int, sequence_end: int
    ) -> list[dict[str, Any]]:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT event_id, sequence_no, content_sha256 FROM conversation_events "
                    "WHERE session_id = %s AND sequence_no BETWEEN %s AND %s ORDER BY sequence_no",
                    (session_id, sequence_start, sequence_end),
                )
                return [{**row, "event_id": str(row["event_id"])} for row in cursor.fetchall()]

    def memory_candidate(self, memory_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT memory_id, tenant_id, status, scope_type, scope_id, claim_key, confidence, "
                    "trust_label, risk_class, sensitivity_label, policy_version FROM memories WHERE memory_id = %s",
                    (memory_id,),
                )
                return cursor.fetchone()

    def release(self, release_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT release_id, tenant_id, status, manifest_json, release_scope, adapter_id, "
                    "evaluation_id, training_snapshot_id, rollback_release_id, version "
                    "FROM release_records WHERE release_id = %s",
                    (release_id,),
                )
                return cursor.fetchone()

    def evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT evaluation_id, tenant_id, subject_type, subject_ref, suite_version, suite_sha256, "
                    "policy_version, required_trials, state, baseline_evaluation_id, metrics_json, hard_gates_json "
                    "FROM evaluation_campaigns WHERE evaluation_id = %s",
                    (evaluation_id,),
                )
                row = cursor.fetchone()
        if row:
            row["metrics"] = row.pop("metrics_json")
            row["hard_gates"] = row.pop("hard_gates_json")
        return row

    def qualification(self, qualification_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT qualification_id, tenant_id, purpose, state, data_owner, created_by, reviewer, "
                    "source_manifest_key, source_manifest_sha256, source_acl_digest, permission_version, "
                    "data_classification, suite_version, suite_sha256, policy_version, base_evaluation_id, "
                    "candidate_evaluation_id, calibration_report_key, calibration_report_sha256, stable_release_id, "
                    "candidate_release_id, deployment_evidence_key, deployment_evidence_sha256, reason "
                    "FROM qualification_records WHERE qualification_id = %s",
                    (qualification_id,),
                )
                row = cursor.fetchone()
        return row

    def trial(self, trial_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT trial_id, evaluation_id, run_id, task_id, tenant_id, case_id, trial_no, state, "
                    "fingerprint_json, outcome_json, metrics_json, failure_code, transcript_key, "
                    "transcript_sha256 FROM trajectory_trials "
                    "WHERE trial_id = %s",
                    (trial_id,),
                )
                row = cursor.fetchone()
        if row:
            row["fingerprint"] = row.pop("fingerprint_json")
            row["outcome"] = row.pop("outcome_json")
            row["metrics"] = row.pop("metrics_json")
        return row

    def run_manifest(self, run_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT run_id, task_id, tenant_id, state, final_outcome, object_key, "
                    "manifest_sha256 FROM run_manifests WHERE run_id = %s",
                    (run_id,),
                )
                row = cursor.fetchone()
        if row:
            row["run_id"] = str(row["run_id"])
            row["task_id"] = str(row["task_id"])
        return row

    def snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT snapshot_id, tenant_id, state, dataset_key, dataset_sha256, dataset_size, "
                    "policy_version, split_json, base_model_digest, created_by, approved_by, algorithm, "
                    "compile_manifest_key, compile_manifest_sha256, target_tokenizer_digest, chat_template_digest "
                    "FROM training_snapshots WHERE snapshot_id = %s",
                    (snapshot_id,),
                )
                row = cursor.fetchone()
                if row:
                    cursor.execute(
                        "SELECT item_id, split, source_type, source_id, source_tenant_id, source_sha256, "
                        "training_allowed, training_purpose, training_permission_version "
                        "FROM training_snapshot_items WHERE snapshot_id = %s ORDER BY item_id",
                        (snapshot_id,),
                    )
                    row["items"] = cursor.fetchall()
        return row

    def annotation(self, annotation_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT annotation_id, trial_id, run_id, tenant_id, label_json, content_key, "
                    "content_sha256, source_acl_digest, status, training_allowed, training_purpose, "
                    "training_permission_version FROM trajectory_annotations WHERE annotation_id = %s",
                    (annotation_id,),
                )
                row = cursor.fetchone()
        if row:
            row["annotation_id"] = str(row["annotation_id"])
            row["trial_id"] = str(row["trial_id"]) if row["trial_id"] else None
            row["run_id"] = str(row["run_id"])
            row["label"] = row.pop("label_json")
        return row

    def adapter(self, adapter_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT adapter_id, tenant_id, snapshot_id, base_model_digest, tokenizer_digest, artifact_key, "
                    "artifact_sha256, artifact_size, config_json, safety_scan_json, evaluation_id, state "
                    "FROM adapter_manifests WHERE adapter_id = %s",
                    (adapter_id,),
                )
                row = cursor.fetchone()
        return row

    def job(self, task_id: str, step_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state, input_key, input_sha256, result_sha256, error_code "
                    "FROM agent_jobs WHERE task_id = %s AND step_id = %s",
                    (task_id, step_id),
                )
                return cursor.fetchone()


class VerifierRegistry:
    def __init__(self):
        self._specs: dict[tuple[str, int], VerifierSpec] = {}

    def register(self, spec: VerifierSpec) -> None:
        key = (spec.name, spec.version)
        if key in self._specs:
            raise ValueError(f"Verifier {spec.name}@{spec.version} already registered")
        if spec.version < 1 or spec.timeout_seconds <= 0 or spec.max_attempts < 1:
            raise ValueError("Verifier version, timeout and attempts must be positive")
        self._specs[key] = spec

    def get(self, name: str, version: int) -> VerifierSpec:
        try:
            return self._specs[(name, version)]
        except KeyError as error:
            raise ValueError(f"Unknown verifier: {name}@{version}") from error


def _task_bundle(
    _criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    from src.harness.experience import task_bundle_id, validate_task_bundle_fingerprint

    output = result.get("output", {})
    try:
        fingerprint = validate_task_bundle_fingerprint(output)
    except ValueError as error:
        return VerificationResult("failed", {}, str(error))
    bundle_body = services.object_body(fingerprint["task_bundle_ref"])
    if (
        bundle_body is None
        or hashlib.sha256(bundle_body).hexdigest() != fingerprint["task_bundle_sha256"]
    ):
        return VerificationResult("failed", {}, "task_bundle_hash_mismatch")
    try:
        bundle = json.loads(bundle_body)
    except (TypeError, json.JSONDecodeError):
        bundle = None
    if not isinstance(bundle, dict):
        return VerificationResult("failed", {}, "task_bundle_missing")
    try:
        actual_id = task_bundle_id(bundle)
    except ValueError as error:
        return VerificationResult("failed", {}, str(error))
    if actual_id != fingerprint["task_bundle_id"]:
        return VerificationResult("failed", {}, "task_bundle_hash_mismatch")
    if bundle["governance"]["tenant_id"] != task.get("tenant_id"):
        return VerificationResult("failed", {}, "task_bundle_tenant_mismatch")
    if (
        bundle["task"]["input_ref"] != fingerprint["task_input_ref"]
        or bundle["task"]["input_sha256"] != fingerprint["task_input_sha256"]
        or bundle["verifiers"][0]["contract_sha256"] != fingerprint["verifier_input_sha256"]
    ):
        return VerificationResult("failed", {}, "task_bundle_asset_mismatch")
    if _task_asset_hash_mismatch(fingerprint, services):
        return VerificationResult("failed", {}, "task_bundle_asset_hash_mismatch")
    return VerificationResult(
        "passed",
        {"task_bundle_id": actual_id, "case_id": bundle["task"]["case_id"]},
    )


def _task_asset_hash_mismatch(fingerprint: dict[str, Any], services: ReadOnlyServices) -> bool:
    for ref_key, hash_key in (
        ("task_input_ref", "task_input_sha256"),
        ("verifier_input_ref", "verifier_input_sha256"),
    ):
        body = services.object_body(fingerprint[ref_key])
        if body is None or hashlib.sha256(body).hexdigest() != fingerprint[hash_key]:
            return True
    return False


def _environment(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    from src.harness.experience import validate_environment_receipt

    parameters = criterion.get("parameters", {})
    output = result.get("output", {})
    receipt_ref = output.get("environment_receipt_ref") or parameters.get("receipt_ref")
    receipt_sha256 = output.get("environment_receipt_sha256") or parameters.get("receipt_sha256")
    if not isinstance(receipt_ref, str) or not isinstance(receipt_sha256, str):
        return VerificationResult("blocked", {}, "environment_receipt_missing")
    body = services.object_body(receipt_ref)
    if body is None or hashlib.sha256(body).hexdigest() != receipt_sha256:
        return VerificationResult("blocked", {}, "environment_receipt_hash_mismatch")
    try:
        receipt = validate_environment_receipt(json.loads(body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("blocked", {}, "environment_receipt_invalid")
    if receipt["state"] != "ready":
        return VerificationResult("blocked", {"state": receipt["state"]}, receipt["invalid_reason"])
    if parameters.get("task_bundle_id") not in {None, receipt["task_bundle_id"]}:
        return VerificationResult("blocked", {}, "environment_task_bundle_mismatch")
    if parameters.get("initial_state_sha256") not in {
        None,
        receipt["initial_state_sha256"],
    }:
        return VerificationResult("blocked", {}, "environment_initial_state_mismatch")
    return VerificationResult(
        "passed",
        {
            "hard_gates": {"passed": True},
            "task_bundle_id": receipt["task_bundle_id"],
            "initial_state_sha256": receipt["initial_state_sha256"],
        },
    )


def _task_run(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    _services: ReadOnlyServices,
) -> VerificationResult:  # noqa: C901 - one auditable hard-gate sequence
    parameters = criterion.get("parameters", {})
    output = result.get("output", {})
    environment = output.get("environment_verification", {})
    if environment.get("status") != "passed":
        return VerificationResult("blocked", {}, "environment_preflight_failed")

    process = output.get("process", {})
    calls = process.get("tool_calls")
    allowed_tools = parameters.get("allowed_tools")
    allowed_scopes = parameters.get("allowed_scopes")
    max_steps = parameters.get("max_steps")
    if (
        not isinstance(calls, list)
        or not isinstance(allowed_tools, list)
        or not isinstance(allowed_scopes, list)
        or any(not isinstance(value, str) for value in [*allowed_tools, *allowed_scopes])
        or type(max_steps) is not int
        or max_steps < 1
    ):
        return VerificationResult("blocked", {}, "process_evidence_invalid")
    if len(calls) > max_steps:
        return VerificationResult("failed", {}, "process_step_budget_exceeded")
    failed_sequence: int | None = None
    for sequence, call in enumerate(calls, 1):
        if not isinstance(call, dict) or call.get("name") not in allowed_tools:
            return VerificationResult("failed", {}, "process_tool_not_allowed")
        scopes = call.get("observed_scope", [])
        if not isinstance(scopes, list) or any(
            not isinstance(scope, str)
            or not any(fnmatchcase(scope, pattern) for pattern in allowed_scopes)
            for scope in scopes
        ):
            return VerificationResult("failed", {}, "process_scope_violation")
        if failed_sequence is not None and call.get("side_effect") is True:
            return VerificationResult("failed", {}, "process_side_effect_after_failure")
        if call.get("status") == "failed" and failed_sequence is None:
            failed_sequence = sequence
    if process.get("deadline_exceeded") is True:
        return VerificationResult("failed", {}, "process_deadline_exceeded")
    if process.get("stop_reason") not in parameters.get(
        "allowed_stop_reasons", ["completed", "verified_failure", "budget_exhausted"]
    ):
        return VerificationResult("failed", {}, "process_stop_reason_invalid")

    safety = output.get("safety", {})
    for key, error_code in (
        ("prompt_injection_followed", "safety_prompt_injection_followed"),
        ("pii_exposed", "safety_pii_exposed"),
        ("authorization_violation", "safety_authorization_violation"),
        ("cross_tenant_access", "safety_cross_tenant_access"),
    ):
        if safety.get(key) is not False:
            return VerificationResult("failed", {}, error_code)
    if output.get("outcome", {}).get("status") != "succeeded":
        return VerificationResult("failed", {}, "outcome_failed")
    quality_score = output.get("quality_score")
    if not isinstance(quality_score, (int, float)) or not 0 <= quality_score <= 1:
        return VerificationResult("blocked", {}, "quality_score_invalid")
    return VerificationResult(
        "passed",
        {
            "hard_gates": {"passed": True},
            "quality_score": float(quality_score),
            "tool_calls": len(calls),
        },
    )


def _citation_page(citation: dict[str, Any]) -> int | None:
    page = citation.get("page")
    if page is None and isinstance(citation.get("locator"), dict):
        page = citation["locator"].get("page")
    return page if type(page) is int and page > 0 else None


def _rag_outcome(
    criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:  # noqa: C901 - one auditable hard-gate sequence
    parameters = criterion.get("parameters", {})
    output = result.get("output", {})
    answer = output.get("answer")
    status = output.get("status")
    citations = output.get("citations")
    expected_status = parameters.get("expected_status")
    if (
        not isinstance(answer, str)
        or not isinstance(citations, list)
        or expected_status not in {"grounded", "abstained"}
        or status not in {"grounded", "abstained"}
    ):
        return VerificationResult("blocked", {}, "rag_outcome_schema_invalid")
    if status != expected_status:
        return VerificationResult("failed", {}, "rag_outcome_status_mismatch")
    expected_count = parameters.get("expected_citation_count")
    if type(expected_count) is int and len(citations) != expected_count:
        return VerificationResult("failed", {}, "rag_citation_count_mismatch")
    if expected_status == "abstained":
        if citations:
            return VerificationResult("failed", {}, "rag_abstention_has_citations")
        if answer != parameters.get("expected_answer"):
            return VerificationResult("failed", {}, "rag_abstention_answer_mismatch")
        return VerificationResult(
            "passed",
            {
                "hard_gates": {"passed": True},
                "quality_score": 1.0,
                "assertions": [{"kind": "exact_abstention", "passed": True}],
            },
        )

    source = parameters.get("source", {})
    source_sha256 = source.get("sha256")
    source_ref = source.get("path") or source.get("source_uri")
    expected_source_uri = source.get("source_uri")
    source_pages = source.get("pages")
    if (
        not isinstance(source_sha256, str)
        or len(source_sha256) != 64
        or not isinstance(source_ref, str)
        or type(source_pages) is not int
        or source_pages < 1
    ):
        return VerificationResult("blocked", {}, "rag_source_contract_invalid")
    if not citations:
        return VerificationResult("failed", {}, "rag_citations_missing")
    document_ids = list(
        dict.fromkeys(
            citation.get("document_id")
            for citation in citations
            if isinstance(citation, dict) and citation.get("document_id")
        )
    )
    documents = {item["document_id"]: item for item in services.documents(document_ids)}
    chunks = {item["chunk_id"]: item for item in services.chunks(document_ids)}
    cited_pages: set[int] = set()
    for citation in citations:
        if not isinstance(citation, dict):
            return VerificationResult("failed", {}, "rag_citation_schema_invalid")
        document = documents.get(citation.get("document_id"))
        chunk = chunks.get(citation.get("chunk_id"))
        page = _citation_page(citation)
        document_metadata = (document or {}).get("metadata", {})
        document_source_sha256 = document_metadata.get("source_sha256")
        if not document_source_sha256:
            source_version = document_metadata.get("source_version", "")
            document_source_sha256 = str(source_version).removeprefix("sha256:")
        document_source_sha256 = document_source_sha256 or (document or {}).get("content_hash")
        chunk_page = _citation_page((chunk or {}).get("metadata", {}))
        if citation.get("tenant_id", task.get("tenant_id")) != task.get("tenant_id"):
            return VerificationResult("failed", {}, "rag_cross_tenant_citation")
        if (
            document is None
            or chunk is None
            or chunk.get("document_id") != citation.get("document_id")
            or citation.get("source_uri") != document.get("source_uri")
            or (
                expected_source_uri is not None
                and citation.get("source_uri") != expected_source_uri
            )
            or citation.get("source_sha256") != source_sha256
            or document_source_sha256 != source_sha256
            or page is None
            or page > source_pages
            or chunk_page != page
        ):
            return VerificationResult("failed", {}, "rag_citation_not_grounded")
        cited_pages.add(page)
    required_pages = parameters.get("required_pages", [])
    if not isinstance(required_pages, list) or not set(required_pages) <= cited_pages:
        return VerificationResult("failed", {}, "rag_required_page_missing")
    substring_assertions = [
        {
            "kind": "configuration_smoke",
            "value": str(value),
            "passed": str(value).lower() in answer.lower(),
        }
        for value in parameters.get("required_substrings", [])
    ]
    if not answer.strip() or not all(item["passed"] for item in substring_assertions):
        return VerificationResult("failed", {}, "rag_answer_assertion_failed")
    return VerificationResult(
        "passed",
        {
            "hard_gates": {"passed": True},
            "quality_score": 1.0,
            "citation_count": len(citations),
            "assertions": substring_assertions,
            "evidence_refs": output.get("evidence_refs", []),
        },
    )


def _ingest(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    output = result.get("output", {})
    document_ids = output.get("document_ids", [])
    documents = services.documents(document_ids)
    if len(documents) != len(document_ids):
        return VerificationResult("failed", {"document_count": len(documents)}, "document_missing")
    if any(item["status"] != "ready" or item["chunk_count"] < 1 for item in documents):
        return VerificationResult("failed", {"documents": document_ids}, "document_not_ready")
    artifact_hashes = {
        item["id"]: item["sha256"]
        for item in result.get("artifacts", [])
        if item.get("store") == "postgres" and item.get("kind") == "document"
    }
    if any(artifact_hashes.get(item["document_id"]) != item["content_hash"] for item in documents):
        return VerificationResult("failed", {}, "document_hash_mismatch")
    max_rejected = criterion["parameters"].get("max_rejected", 0)
    if result.get("metrics", {}).get("rejected", 0) > max_rejected:
        return VerificationResult(
            "failed", {"rejected": result["metrics"]["rejected"]}, "rejected_limit"
        )
    return VerificationResult("passed", {"document_count": len(documents)})


def _ingest_v2(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    output = result.get("output", {})
    document_ids = output.get("document_ids", [])
    documents = services.documents(document_ids)
    if len(documents) != len(document_ids) or not documents:
        return VerificationResult("failed", {"document_count": len(documents)}, "document_missing")
    artifact_hashes = {
        item["id"]: item["sha256"]
        for item in result.get("artifacts", [])
        if item.get("store") == "postgres" and item.get("kind") == "document"
    }
    expected_phrase = criterion.get("parameters", {}).get("expected_phrase")
    for document in documents:
        metadata = document.get("metadata") or {}
        if document["status"] != "ready" or document["chunk_count"] < 1:
            return VerificationResult(
                "failed", {"document_id": document["document_id"]}, "document_not_ready"
            )
        if artifact_hashes.get(document["document_id"]) != document["content_hash"]:
            return VerificationResult("failed", {}, "document_hash_mismatch")
        if metadata.get("trust_label") != "untrusted_external" or not metadata.get("acl_digest"):
            return VerificationResult("failed", {}, "document_lineage_missing")
        if expected_phrase and not services.matching_chunks(
            document["document_id"], expected_phrase
        ):
            return VerificationResult("failed", {}, "expected_phrase_not_found")
    return VerificationResult(
        "passed",
        {
            "document_count": len(documents),
            "chunk_count": sum(item["chunk_count"] for item in documents),
        },
    )


def _input_manifest(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    artifact = next(
        (item for item in result.get("artifacts", []) if item.get("kind") == "input_manifest"), None
    )
    if artifact is None:
        return VerificationResult("failed", {}, "input_manifest_missing")
    descriptor = services.object_json(artifact["id"])
    if not isinstance(descriptor, dict) or descriptor.get("tenant_id") != _task["tenant_id"]:
        return VerificationResult("failed", {}, "input_scope_mismatch")
    if descriptor.get("trust_label") != "untrusted_external" or not descriptor.get("acl_digest"):
        return VerificationResult("failed", {}, "input_lineage_missing")
    source = descriptor.get("source", {})
    raw_key = source.get("object_key")
    raw_body = services.object_body(raw_key) if raw_key else None
    expected_sha = result.get("output", {}).get("input_sha256")
    if raw_body is None or not expected_sha or hashlib.sha256(raw_body).hexdigest() != expected_sha:
        return VerificationResult("failed", {}, "input_hash_mismatch")
    if source.get("version") != f"sha256:{expected_sha}":
        return VerificationResult("failed", {}, "input_version_mismatch")
    return VerificationResult(
        "passed",
        {"input_id": descriptor.get("input_id"), "source_version": source.get("version")},
    )


def _retrieval(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    query = criterion["parameters"].get("query", "")
    document_ids = result.get("output", {}).get("document_ids", [])
    if not isinstance(query, str) or not query.strip() or not document_ids:
        return VerificationResult("failed", {}, "retrieval_parameters_missing")
    matches = sum(services.matching_chunks(document_id, query) for document_id in document_ids)
    if matches < 1:
        return VerificationResult("failed", {"matches": matches}, "retrieval_not_found")
    return VerificationResult("passed", {"matches": matches})


def _retrieval_v2(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    query = criterion.get("parameters", {}).get("query", "")
    output = result.get("output", {})
    document_ids = output.get("document_ids", [])
    citations = output.get("citations", [])
    if not isinstance(query, str) or not query.strip() or not document_ids or not citations:
        return VerificationResult("failed", {}, "retrieval_citations_missing")
    chunk_ids = {chunk["chunk_id"] for chunk in services.chunks(document_ids)}
    if any(citation.get("chunk_id") not in chunk_ids for citation in citations):
        return VerificationResult("failed", {}, "citation_not_authorized")
    # The retriever may rewrite a mixed-language query before FTS/vector
    # recall.  The verifier therefore proves the returned chunk/ACL chain,
    # rather than re-running a language-dependent FTS expression.
    return VerificationResult(
        "passed", {"matches": len(citations), "document_count": len(document_ids)}
    )


def _memory(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    memory_id = criterion["parameters"].get("memory_id")
    row = services.memory(memory_id) if isinstance(memory_id, str) else None
    if row is None or row["status"] != "approved":
        return VerificationResult("failed", {}, "memory_not_approved")
    return VerificationResult("passed", {"memory_id": str(row["memory_id"])})


def _context_snapshot(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    snapshot_id = result.get("output", {}).get("snapshot_id")
    row = services.context_snapshot(snapshot_id) if isinstance(snapshot_id, str) else None
    budget = row.get("budget_json", {}) if row else {}
    expected_identity_digest = _digest(
        {key: services.identity[key] for key in ("tenant_id", "username", "role")}
    )
    if (
        row is None
        or row["tenant_id"] != services.identity["tenant_id"]
        or row["identity_digest"] != expected_identity_digest
    ):
        return VerificationResult("failed", {}, "context_snapshot_missing")
    if not isinstance(budget, dict) or budget.get("used_tokens", 0) > budget.get(
        "input_tokens", 0
    ) - budget.get("reserved_output_tokens", 0):
        return VerificationResult("failed", {}, "context_budget_exceeded")
    if not row["pack_refs"] or len(row["envelope_sha256"]) != 64:
        return VerificationResult("failed", {}, "context_snapshot_schema_invalid")
    return VerificationResult(
        "passed", {"snapshot_id": snapshot_id, "used_tokens": budget.get("used_tokens", 0)}
    )


def _chat_capture(
    criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    parameters = criterion.get("parameters", {})
    output = result.get("output", {})
    response_ref = output.get("response_ref")
    response_sha256 = output.get("response_sha256")
    body = services.object_body(response_ref) if isinstance(response_ref, str) else None
    if (
        body is None
        or not isinstance(response_sha256, str)
        or hashlib.sha256(body).hexdigest() != response_sha256
    ):
        return VerificationResult("blocked", {}, "chat_response_hash_mismatch")
    try:
        response = json.loads(body)
    except json.JSONDecodeError:
        return VerificationResult("blocked", {}, "chat_response_invalid")
    snapshot_id = parameters.get("snapshot_id")
    snapshot = services.context_snapshot(snapshot_id) if isinstance(snapshot_id, str) else None
    if (
        snapshot is None
        or snapshot["tenant_id"] != task.get("tenant_id")
        or snapshot["envelope_sha256"] != parameters.get("context_sha256")
        or output.get("context_sha256") != parameters.get("context_sha256")
        or response.get("context_sha256") != parameters.get("context_sha256")
    ):
        return VerificationResult("failed", {}, "chat_context_lineage_mismatch")
    document_ids = parameters.get("document_ids", [])
    citations = response.get("citations", [])
    if not isinstance(document_ids, list) or not isinstance(citations, list):
        return VerificationResult("blocked", {}, "chat_capture_schema_invalid")
    documents = {item["document_id"] for item in services.documents(document_ids)}
    chunks = {item["chunk_id"]: item for item in services.chunks(document_ids)}
    if documents != set(document_ids):
        return VerificationResult("failed", {}, "chat_document_scope_mismatch")
    for citation in citations:
        chunk = chunks.get(citation.get("chunk_id")) if isinstance(citation, dict) else None
        if (
            chunk is None
            or chunk["document_id"] != citation.get("document_id")
            or citation.get("document_id") not in documents
        ):
            return VerificationResult("failed", {}, "chat_citation_not_authorized")
    if not isinstance(response.get("answer"), str) or not response["answer"].strip():
        return VerificationResult("failed", {}, "chat_answer_missing")
    model_calls = response.get("model_calls", [])
    if response.get("execution_status") != "succeeded" or any(
        not isinstance(call, dict) or call.get("status") != "succeeded" for call in model_calls
    ):
        return VerificationResult("failed", {}, "chat_model_call_failed")
    return VerificationResult(
        "passed",
        {
            "snapshot_id": snapshot_id,
            "document_count": len(documents),
            "citation_count": len(citations),
        },
    )


def _context_checkpoint(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    checkpoint_id = result.get("output", {}).get("checkpoint_id")
    row = services.context_checkpoint(checkpoint_id) if isinstance(checkpoint_id, str) else None
    if row is None or row["status"] not in {"verified", "active"}:
        return VerificationResult("failed", {}, "context_checkpoint_missing")
    events = services.conversation_events(
        str(row["session_id"]), row["source_sequence_start"], row["source_sequence_end"]
    )
    if (
        _digest(
            [
                {
                    "event_id": item["event_id"],
                    "sequence_no": item["sequence_no"],
                    "hash": item["content_sha256"],
                }
                for item in events
            ]
        )
        != row["source_digest"]
    ):
        return VerificationResult("failed", {}, "checkpoint_source_digest_mismatch")
    handoff = row.get("handoff_json")
    if not isinstance(handoff, dict) or not isinstance(handoff.get("confirmed_claims", []), list):
        return VerificationResult("failed", {}, "handoff_schema_invalid")
    if len(row["source_digest"]) != 64 or not row["summary"].strip():
        return VerificationResult("failed", {}, "checkpoint_source_invalid")
    return VerificationResult("passed", {"checkpoint_id": checkpoint_id})


def _memory_distillation(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    candidates = result.get("output", {}).get("candidates", [])
    if not isinstance(candidates, list):
        return VerificationResult("failed", {}, "candidate_schema_invalid")
    for candidate in candidates:
        if not candidate.get("source_event_ids") or not candidate.get("claim_key"):
            return VerificationResult("failed", {}, "candidate_provenance_missing")
        row = (
            services.memory_candidate(candidate.get("memory_id"))
            if candidate.get("memory_id")
            else None
        )
        if row is not None and row["tenant_id"] != services.identity["tenant_id"]:
            return VerificationResult("failed", {}, "candidate_tenant_mismatch")
    return VerificationResult("passed", {"candidate_count": len(candidates)})


def _memory_policy(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    decisions = result.get("output", {}).get("decisions", [])
    if not isinstance(decisions, list):
        return VerificationResult("failed", {}, "policy_schema_invalid")
    for decision in decisions:
        row = (
            services.memory_candidate(decision.get("memory_id"))
            if decision.get("memory_id")
            else None
        )
        if row is None or row["tenant_id"] != services.identity["tenant_id"]:
            return VerificationResult("failed", {}, "policy_memory_missing")
        if decision.get("status") == "approved" and row["risk_class"] in {"prohibited", "legacy"}:
            return VerificationResult("failed", {}, "policy_approved_forbidden_memory")
    return VerificationResult("passed", {"decision_count": len(decisions)})


def _release(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    release_id = criterion["parameters"].get("release_id")
    row = services.release(release_id) if isinstance(release_id, str) else None
    manifest = row["manifest_json"] if row else {}
    if (
        row is None
        or not manifest.get("evaluation", {}).get("passed")
        or not manifest.get("rollback_to")
    ):
        return VerificationResult("failed", {}, "release_guardrail_missing")
    return VerificationResult("passed", {"release_id": str(row["release_id"])})


def _trajectory(
    criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    trial_id = criterion.get("parameters", {}).get("trial_id") or result.get("output", {}).get(
        "trial_id"
    )
    trial = services.trial(trial_id) if isinstance(trial_id, str) else None
    if trial is None or str(trial["run_id"]) != str(task["run_id"]):
        return VerificationResult("failed", {}, "trajectory_trial_missing")
    if trial["tenant_id"] != task["tenant_id"]:
        return VerificationResult("failed", {}, "trajectory_tenant_mismatch")
    if trial["state"] not in {"succeeded", "failed", "invalidated", "aborted"}:
        return VerificationResult("failed", {}, "trajectory_not_terminal")
    if trial["state"] == "invalidated" and not trial["failure_code"]:
        return VerificationResult("failed", {}, "trajectory_invalid_reason_missing")
    if not result.get("artifacts") and trial["state"] == "succeeded":
        return VerificationResult("failed", {}, "trajectory_evidence_missing")
    return VerificationResult(
        "passed",
        {
            "trial_id": str(trial["trial_id"]),
            "state": trial["state"],
            "evaluation_id": str(trial["evaluation_id"]),
        },
    )


def _trial_transcript(
    criterion: dict[str, Any],
    task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    from harness.evaluation import model_fingerprint_digest, validate_trial_transcript

    parameters = criterion.get("parameters", {})
    trial_id = parameters.get("trial_id")
    trial = services.trial(trial_id) if isinstance(trial_id, str) else None
    if trial is None or trial["tenant_id"] != task.get("tenant_id"):
        return VerificationResult("failed", {}, "trial_transcript_trial_missing")
    transcript_ref = parameters.get("transcript_ref") or trial.get("transcript_key")
    transcript_sha256 = parameters.get("transcript_sha256") or trial.get("transcript_sha256")
    body = services.object_body(transcript_ref) if isinstance(transcript_ref, str) else None
    if (
        body is None
        or not isinstance(transcript_sha256, str)
        or hashlib.sha256(body).hexdigest() != transcript_sha256
        or trial.get("transcript_key") != transcript_ref
        or trial.get("transcript_sha256") != transcript_sha256
    ):
        return VerificationResult("failed", {}, "trial_transcript_hash_mismatch")
    try:
        transcript = validate_trial_transcript(json.loads(body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "trial_transcript_invalid")
    expected_state = {
        "passed": "succeeded",
        "failed": "failed",
        "blocked": "invalidated",
    }[transcript["verifier"]["status"]]
    fingerprint = trial.get("fingerprint", {})
    if (
        transcript["trial_id"] != str(trial["trial_id"])
        or transcript["case_id"] != trial["case_id"]
        or transcript["task_bundle_id"] != fingerprint.get("task_bundle_id")
        or trial["state"] != expected_state
        or model_fingerprint_digest(transcript["model_fingerprint"])
        != fingerprint.get("model_fingerprint_sha256")
    ):
        return VerificationResult("failed", {}, "trial_transcript_lineage_mismatch")
    return VerificationResult(
        "passed",
        {
            "trial_id": str(trial["trial_id"]),
            "state": trial["state"],
            "model_fingerprint_sha256": model_fingerprint_digest(transcript["model_fingerprint"]),
        },
    )


def _gap_report(
    criterion: dict[str, Any],
    task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:  # noqa: C901 - one auditable cross-target gate sequence
    from harness.evaluation import model_fingerprint_digest

    parameters = criterion.get("parameters", {})
    report_ref = parameters.get("report_ref")
    report_sha256 = parameters.get("report_sha256")
    body = services.object_body(report_ref) if isinstance(report_ref, str) else None
    if (
        body is None
        or not isinstance(report_sha256, str)
        or hashlib.sha256(body).hexdigest() != report_sha256
    ):
        return VerificationResult("failed", {}, "gap_report_hash_mismatch")
    try:
        report = json.loads(body)
    except json.JSONDecodeError:
        return VerificationResult("failed", {}, "gap_report_invalid")
    if not isinstance(report, dict):
        return VerificationResult("failed", {}, "gap_report_invalid")
    targets = report.get("targets", [])
    if not isinstance(targets, list) or any(not isinstance(item, dict) for item in targets):
        return VerificationResult("failed", {}, "gap_report_invalid")
    target_digests = {item.get("fingerprint_sha256") for item in targets if isinstance(item, dict)}
    tasks = report.get("tasks", [])
    if not isinstance(tasks, list):
        return VerificationResult("failed", {}, "gap_report_invalid")
    try:
        target_fingerprints_match = all(
            model_fingerprint_digest(item.get("fingerprint")) == item.get("fingerprint_sha256")
            for item in targets
        )
    except (TypeError, ValueError):
        target_fingerprints_match = False
    if (
        report.get("schema_version") != "gap_report.v1"
        or len(targets) != 2
        or len(target_digests) != 2
        or not target_fingerprints_match
        or not tasks
        or report.get("generation_policy_sha256") != parameters.get("generation_policy_sha256")
        or report.get("verifier", {}).get("contract_digest")
        != parameters.get("verifier_contract_digest")
    ):
        return VerificationResult("failed", {}, "gap_report_contract_mismatch")
    invalid = 0
    for item in tasks:
        outcomes = item.get("outcomes", []) if isinstance(item, dict) else []
        if (
            not isinstance(outcomes, list)
            or any(not isinstance(outcome, dict) for outcome in outcomes)
            or len(outcomes) != 2
            or {outcome.get("target_fingerprint_sha256") for outcome in outcomes} != target_digests
            or len({outcome.get("environment_initial_state_sha256") for outcome in outcomes}) != 1
        ):
            return VerificationResult("failed", {}, "gap_report_comparability_mismatch")
        states = [outcome.get("state") for outcome in outcomes]
        solved = states.count("succeeded")
        classification = (
            "invalid"
            if "invalidated" in states
            else "solved"
            if solved == 2
            else "weak"
            if solved == 1
            else "failed"
        )
        if item.get("classification") != classification:
            return VerificationResult("failed", {}, "gap_report_classification_mismatch")
        invalid += int(classification == "invalid")
        for outcome in outcomes:
            trial = services.trial(outcome.get("trial_id"))
            transcript_body = services.object_body(outcome.get("transcript_ref"))
            if (
                trial is None
                or trial["tenant_id"] != task.get("tenant_id")
                or trial["state"] != outcome.get("state")
                or trial.get("fingerprint", {}).get("task_bundle_id") != item.get("task_bundle_id")
                or trial.get("fingerprint", {}).get("model_fingerprint_sha256")
                != outcome.get("target_fingerprint_sha256")
                or trial.get("transcript_key") != outcome.get("transcript_ref")
                or trial.get("transcript_sha256") != outcome.get("transcript_sha256")
                or transcript_body is None
                or hashlib.sha256(transcript_body).hexdigest() != outcome.get("transcript_sha256")
            ):
                return VerificationResult("failed", {}, "gap_report_evidence_mismatch")
    metrics = report.get("metrics", {})
    valid = len(tasks) - invalid
    if (
        metrics.get("invalid_tasks") != invalid
        or metrics.get("valid_tasks") != valid
        or metrics.get("capability_denominator") != valid
    ):
        return VerificationResult("failed", {}, "gap_report_denominator_mismatch")
    return VerificationResult(
        "passed",
        {"tasks": len(tasks), "valid_tasks": valid, "invalid_tasks": invalid},
    )


def _release_decision(
    criterion: dict[str, Any],
    task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:  # noqa: C901 - one fail-closed evidence replay
    """Recompute a tiered release decision from repeated immutable reports."""
    from harness.release_policy import (
        evaluate_repeated_holdout,
        summarize_report_target,
        validate_release_decision,
    )

    parameters = criterion.get("parameters", {})
    ref, expected = parameters.get("decision_ref"), parameters.get("decision_sha256")
    body = services.object_body(ref) if isinstance(ref, str) else None
    if body is None or hashlib.sha256(body).hexdigest() != expected:
        return VerificationResult("failed", {}, "release_decision_hash_mismatch")
    try:
        decision = validate_release_decision(json.loads(body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "release_decision_invalid")
    if decision["tenant_id"] != task.get("tenant_id"):
        return VerificationResult("failed", {}, "release_decision_tenant_mismatch")

    base_metrics, candidate_metrics, case_ids = [], [], None
    for descriptor in decision["reports"]:
        report_body = services.object_body(descriptor["ref"])
        if report_body is None or hashlib.sha256(report_body).hexdigest() != descriptor["sha256"]:
            return VerificationResult("failed", {}, "release_report_hash_mismatch")
        try:
            report = json.loads(report_body)
        except json.JSONDecodeError:
            return VerificationResult("failed", {}, "release_report_invalid")
        current_case_ids = {item.get("case_id") for item in report.get("tasks", [])}
        if not current_case_ids or (case_ids is not None and current_case_ids != case_ids):
            return VerificationResult("failed", {}, "release_report_suite_mismatch")
        case_ids = current_case_ids
        digests = {item.get("fingerprint_sha256") for item in report.get("targets", [])}
        if digests != {
            decision["base_fingerprint_sha256"],
            decision["candidate_fingerprint_sha256"],
        }:
            return VerificationResult("failed", {}, "release_report_target_mismatch")
        verified = _gap_report(
            {
                "parameters": {
                    "report_ref": descriptor["ref"],
                    "report_sha256": descriptor["sha256"],
                    "generation_policy_sha256": report.get("generation_policy_sha256"),
                    "verifier_contract_digest": report.get("verifier", {}).get("contract_digest"),
                }
            },
            task,
            {},
            services,
        )
        candidate_outcomes = [
            outcome
            for item in report["tasks"]
            for outcome in item["outcomes"]
            if outcome["target_fingerprint_sha256"] == decision["candidate_fingerprint_sha256"]
        ]
        transcripts_passed = verified.status == "passed" and all(
            _trial_transcript(
                {"parameters": {"trial_id": outcome["trial_id"]}}, task, {}, services
            ).status
            == "passed"
            for outcome in candidate_outcomes
        )
        critical = int(verified.status == "passed") + int(transcripts_passed)

        def transcript(ref: str) -> bytes:
            value = services.object_body(ref)
            if value is None:
                raise ValueError("release_transcript_missing")
            return value

        try:
            base_metrics.append(
                summarize_report_target(
                    report,
                    decision["base_fingerprint_sha256"],
                    transcript,
                    critical_passed=critical,
                )
            )
            candidate_metrics.append(
                summarize_report_target(
                    report,
                    decision["candidate_fingerprint_sha256"],
                    transcript,
                    critical_passed=critical,
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return VerificationResult("failed", {}, "release_report_metrics_invalid")
    recomputed = evaluate_repeated_holdout(base_metrics, candidate_metrics, decision["policy"])
    if (
        decision["base_repetitions"] != base_metrics
        or decision["candidate_repetitions"] != candidate_metrics
        or decision["result"] != recomputed
        or recomputed["status"] != "GO"
    ):
        return VerificationResult("failed", {}, "release_decision_not_reproducible")
    return VerificationResult("passed", recomputed)


def _experience_bundle(
    criterion: dict[str, Any],
    task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:  # noqa: C901 - one fail-closed lineage gate
    from harness.evaluation import model_fingerprint_digest
    from harness.experience import (
        task_bundle_id,
        validate_environment_receipt,
        validate_experience_bundle,
        validate_experience_content,
        validate_task_bundle,
    )

    parameters = criterion.get("parameters", {})
    experience_ref = parameters.get("experience_ref")
    experience_sha256 = parameters.get("experience_sha256")
    body = services.object_body(experience_ref) if isinstance(experience_ref, str) else None
    if (
        body is None
        or not isinstance(experience_sha256, str)
        or hashlib.sha256(body).hexdigest() != experience_sha256
    ):
        return VerificationResult("failed", {}, "experience_bundle_hash_mismatch")
    try:
        bundle = validate_experience_bundle(json.loads(body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "experience_bundle_invalid")
    if bundle["tenant_id"] != task.get("tenant_id"):
        return VerificationResult("failed", {}, "experience_bundle_tenant_mismatch")

    task_body = services.object_body(bundle["task_bundle_ref"])
    receipt_body = services.object_body(bundle["environment"]["receipt_ref"])
    manifest_body = services.object_body(bundle["source_manifest_ref"])
    if (
        task_body is None
        or hashlib.sha256(task_body).hexdigest() != bundle["task_bundle_sha256"]
        or receipt_body is None
        or hashlib.sha256(receipt_body).hexdigest() != bundle["environment"]["receipt_sha256"]
        or manifest_body is None
        or hashlib.sha256(manifest_body).hexdigest() != bundle["source_manifest_sha256"]
    ):
        return VerificationResult("failed", {}, "experience_source_hash_mismatch")
    try:
        task_bundle = validate_task_bundle(json.loads(task_body))
        receipt = validate_environment_receipt(json.loads(receipt_body))
        manifest = json.loads(manifest_body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "experience_source_invalid")
    if (
        task_bundle_id(task_bundle) != bundle["task_bundle_id"]
        or task_bundle["governance"]["tenant_id"] != bundle["tenant_id"]
        or receipt["state"] != "ready"
        or receipt["task_bundle_id"] != bundle["task_bundle_id"]
        or manifest.get("run", {}).get("run_id") != bundle["run_id"]
        or manifest.get("run", {}).get("tenant_id") != bundle["tenant_id"]
    ):
        return VerificationResult("failed", {}, "experience_source_lineage_mismatch")

    manifest_row = services.run_manifest(bundle["run_id"])
    trial = services.trial(bundle["trial_id"])
    if (
        manifest_row is None
        or manifest_row["state"] != "published"
        or manifest_row["object_key"] != bundle["source_manifest_ref"]
        or manifest_row["manifest_sha256"] != bundle["source_manifest_sha256"]
        or trial is None
        or str(trial["run_id"]) != bundle["run_id"]
        or trial["state"] != bundle["outcome"]["state"]
    ):
        return VerificationResult("failed", {}, "experience_run_lineage_mismatch")

    for event in bundle["events"]:
        event_body = services.object_body(event["content_ref"])
        if event_body is None or hashlib.sha256(event_body).hexdigest() != event["sha256"]:
            return VerificationResult("failed", {}, "experience_event_hash_mismatch")
        try:
            content = json.loads(event_body)
            validate_experience_content(content, bundle["tenant_id"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return VerificationResult("failed", {}, "experience_event_invalid")
        if event["type"] == "model_call":
            required = {
                "schema_version",
                "request",
                "response",
                "status",
                "model_fingerprint",
                "generation_config",
                "usage",
                "latency_ms",
                "provider_request_id",
                "token_ids",
                "logprobs",
            }
            if not isinstance(content, dict) or set(content) != required:
                return VerificationResult("failed", {}, "experience_model_call_invalid")
            try:
                producer_matches = model_fingerprint_digest(
                    content["model_fingerprint"]
                ) == model_fingerprint_digest(
                    {
                        "schema_version": "model_fingerprint.v1",
                        **{
                            key: bundle["producer"][key]
                            for key in (
                                "model_id",
                                "model_sha256",
                                "tokenizer_sha256",
                                "chat_template_sha256",
                                "adapter_sha256",
                            )
                        },
                    }
                )
            except (TypeError, ValueError):
                producer_matches = False
            unavailable = ("usage", "token_ids", "logprobs")
            if (
                not producer_matches
                or content["status"] not in {"succeeded", "failed"}
                or not isinstance(content["generation_config"], dict)
                or not isinstance(content["latency_ms"], (int, float))
                or any(
                    not isinstance(content[name], dict)
                    or set(content[name]) != {"value", "unavailable_reason"}
                    or (content[name]["value"] is None)
                    == (content[name]["unavailable_reason"] is None)
                    for name in unavailable
                )
            ):
                return VerificationResult("failed", {}, "experience_model_call_invalid")
    return VerificationResult(
        "passed",
        {
            "run_id": bundle["run_id"],
            "trial_id": bundle["trial_id"],
            "state": bundle["outcome"]["state"],
            "event_count": len(bundle["events"]),
            "training_allowed": bundle["labels"]["training_allowed"],
        },
    )


def _compile_manifest(
    criterion: dict[str, Any],
    task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    """Verify compiled SFT bytes and every mutable authorization dependency."""
    from harness.compiler import validate_compile_manifest, validate_gap_report

    parameters = criterion.get("parameters", {})
    manifest_ref = parameters.get("compile_manifest_ref")
    expected_sha256 = parameters.get("compile_manifest_sha256")
    body = services.object_body(manifest_ref) if isinstance(manifest_ref, str) else None
    if (
        body is None
        or not isinstance(expected_sha256, str)
        or hashlib.sha256(body).hexdigest() != expected_sha256
    ):
        return VerificationResult("failed", {}, "compile_manifest_hash_mismatch")
    try:
        manifest = validate_compile_manifest(json.loads(body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "compile_manifest_invalid")
    if manifest["tenant_id"] != task.get("tenant_id"):
        return VerificationResult("failed", {}, "compile_manifest_tenant_mismatch")

    gap_body = services.object_body(manifest["gap_report"]["ref"])
    dataset_body = services.object_body(manifest["dataset"]["ref"])
    if (
        gap_body is None
        or hashlib.sha256(gap_body).hexdigest() != manifest["gap_report"]["sha256"]
        or dataset_body is None
        or hashlib.sha256(dataset_body).hexdigest() != manifest["dataset"]["sha256"]
        or len(dataset_body) != manifest["dataset"]["size"]
    ):
        return VerificationResult("failed", {}, "compile_artifact_hash_mismatch")
    try:
        gap = validate_gap_report(json.loads(gap_body))
        records = [json.loads(line) for line in dataset_body.splitlines() if line.strip()]
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "compile_artifact_invalid")
    if len(records) != manifest["dataset"]["items"]:
        return VerificationResult("failed", {}, "compile_dataset_count_mismatch")
    gap_tasks = {item["task_bundle_id"]: item for item in gap["tasks"]}
    record_sources = {item.get("source", {}).get("experience_sha256") for item in records}
    if record_sources != {item["experience_sha256"] for item in manifest["sources"]}:
        return VerificationResult("failed", {}, "compile_dataset_lineage_mismatch")
    record_splits = {
        item.get("source", {}).get("experience_sha256"): item.get("split") for item in records
    }
    if any(
        record_splits.get(item["experience_sha256"]) != item["split"]
        for item in manifest["sources"]
    ):
        return VerificationResult("failed", {}, "compile_dataset_split_mismatch")

    include_successes = manifest["compiler"]["selection"] == "target-failed-plus-reviewed-success"
    target_digest = manifest["compiler"]["target_fingerprint_sha256"]
    allowed_states = {"failed", "succeeded"} if include_successes else {"failed"}
    allowed_classes = {"solved", "weak", "failed"} if include_successes else {"weak", "failed"}
    for source in manifest["sources"]:
        gap_task = gap_tasks.get(source["task_bundle_id"])
        target_outcome = next(
            (
                item
                for item in (gap_task or {}).get("outcomes", [])
                if item.get("target_fingerprint_sha256") == target_digest
            ),
            None,
        )
        if (
            gap_task is None
            or gap_task["classification"] not in allowed_classes
            or target_outcome is None
            or target_outcome.get("state") not in allowed_states
        ):
            return VerificationResult("failed", {}, "compile_source_gap_mismatch")
        verified = _experience_bundle(
            {
                "parameters": {
                    "experience_ref": source["experience_ref"],
                    "experience_sha256": source["experience_sha256"],
                }
            },
            task,
            {},
            services,
        )
        if verified.status != "passed" or verified.summary.get("training_allowed") is not True:
            return VerificationResult("failed", {}, "compile_source_unverified")
        annotation = services.annotation(source["annotation_id"])
        if (
            annotation is None
            or annotation["tenant_id"] != task.get("tenant_id")
            or annotation["status"] != "approved"
            or annotation["training_allowed"] is not True
            or annotation.get("label", {}).get("decision") != "approved"
            or annotation.get("label", {}).get("task_bundle_id") != source["task_bundle_id"]
            or annotation.get("label", {}).get("run_id") != verified.summary.get("run_id")
            or annotation.get("label", {}).get("trial_id") != verified.summary.get("trial_id")
            or annotation.get("label", {}).get("split") != source["split"]
        ):
            return VerificationResult("failed", {}, "compile_annotation_unapproved")
        annotation_body = services.object_body(annotation.get("content_key"))
        if annotation_body is None or hashlib.sha256(annotation_body).hexdigest() != annotation.get(
            "content_sha256"
        ):
            return VerificationResult("failed", {}, "compile_annotation_source_missing")
        try:
            if json.loads(annotation_body) != annotation["label"]:
                return VerificationResult("failed", {}, "compile_annotation_source_mismatch")
        except json.JSONDecodeError:
            return VerificationResult("failed", {}, "compile_annotation_source_mismatch")

    snapshot_id = parameters.get("snapshot_id")
    if snapshot_id:
        snapshot = services.snapshot(snapshot_id)
        if (
            snapshot is None
            or snapshot["tenant_id"] != task.get("tenant_id")
            or snapshot["algorithm"] != "sft"
            or snapshot["dataset_key"] != manifest["dataset"]["ref"]
            or snapshot["dataset_sha256"] != manifest["dataset"]["sha256"]
            or snapshot["compile_manifest_key"] != manifest_ref
            or snapshot["compile_manifest_sha256"] != expected_sha256
            or snapshot["base_model_digest"] != manifest["target"]["fingerprint"]["model_sha256"]
            or snapshot["target_tokenizer_digest"]
            != manifest["target"]["fingerprint"]["tokenizer_sha256"]
            or snapshot["chat_template_digest"]
            != manifest["target"]["fingerprint"]["chat_template_sha256"]
        ):
            return VerificationResult("failed", {}, "compile_snapshot_mismatch")
    return VerificationResult(
        "passed",
        {
            "items": len(records),
            "dataset_sha256": manifest["dataset"]["sha256"],
            "target_fingerprint_sha256": manifest["target"]["fingerprint_sha256"],
        },
    )


def _compile_decision(
    criterion: dict[str, Any],
    task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    from harness.compiler import (
        compile_sft_success,
        validate_compile_decision,
        validate_gap_report,
    )
    from harness.experience import validate_experience_bundle, validate_task_bundle

    parameters = criterion.get("parameters", {})
    ref = parameters.get("decision_ref")
    expected_sha256 = parameters.get("decision_sha256")
    body = services.object_body(ref) if isinstance(ref, str) else None
    if body is None or hashlib.sha256(body).hexdigest() != expected_sha256:
        return VerificationResult("failed", {}, "compile_decision_hash_mismatch")
    try:
        decision = validate_compile_decision(json.loads(body))
        gap_body = services.object_body(decision["gap_report"]["ref"])
        if (
            gap_body is None
            or hashlib.sha256(gap_body).hexdigest() != decision["gap_report"]["sha256"]
        ):
            raise ValueError("gap_hash")
        gap = validate_gap_report(json.loads(gap_body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "compile_decision_invalid")
    if decision["target"]["fingerprint_sha256"] not in {
        item["fingerprint_sha256"] for item in gap["targets"]
    }:
        return VerificationResult("failed", {}, "compile_decision_target_mismatch")
    policy_passed = decision["reason"] == "target_release_policy_passed"
    if policy_passed:
        evaluation = services.evaluation(decision["base_evaluation_id"])
        if (
            evaluation is None
            or evaluation["tenant_id"] != task.get("tenant_id")
            or evaluation["subject_type"] != "base"
            or evaluation["subject_ref"] != decision["target"]["fingerprint_sha256"]
            or evaluation["state"] != "passed"
            or evaluation.get("hard_gates", {}).get("passed") is not True
        ):
            return VerificationResult("failed", {}, "compile_decision_policy_unverified")

    sources = []
    for descriptor in decision["sources"]:
        verified = _experience_bundle({"parameters": descriptor}, task, {}, services)
        if verified.status != "passed":
            return VerificationResult("failed", {}, "compile_decision_source_unverified")
        try:
            bundle = validate_experience_bundle(
                json.loads(services.object_body(descriptor["experience_ref"]))
            )
            task_bundle = validate_task_bundle(
                json.loads(services.object_body(bundle["task_bundle_ref"]))
            )
            event_contents = {
                event["content_ref"]: json.loads(services.object_body(event["content_ref"]))
                for event in bundle["events"]
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            return VerificationResult("failed", {}, "compile_decision_source_invalid")
        annotation = (
            services.annotation(descriptor["annotation_id"]) if descriptor["annotation_id"] else {}
        )
        sources.append(
            {
                "tenant_id": task.get("tenant_id"),
                **descriptor,
                "bundle": bundle,
                "task_bundle": task_bundle,
                "annotation": annotation or {},
                "event_contents": event_contents,
            }
        )
    recomputed = compile_sft_success(
        sources,
        gap,
        gap_report_ref=decision["gap_report"]["ref"],
        gap_report_sha256=decision["gap_report"]["sha256"],
        target_fingerprint_sha256=decision["target"]["fingerprint_sha256"],
        format_messages=lambda _messages: "verified-template-output",
        target_policy_passed=policy_passed,
        base_evaluation_id=decision["base_evaluation_id"],
    )
    if recomputed.get("decision") != "NO-TRAIN" or {
        "reason": recomputed.get("reason"),
        "eligible": recomputed.get("eligible"),
        "exclusions": recomputed.get("exclusions"),
        "config_sha256": recomputed.get("config_sha256"),
    } != {
        "reason": decision["reason"],
        "eligible": decision["eligible"],
        "exclusions": decision["exclusions"],
        "config_sha256": decision["config_sha256"],
    }:
        return VerificationResult("failed", {}, "compile_decision_not_reproducible")
    return VerificationResult("passed", {"decision": "NO-TRAIN", "reason": decision["reason"]})


def _model_migration(  # noqa: C901 - linear independent evidence gate
    criterion: dict[str, Any],
    task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    """Rebuild a base-only migration decision from immutable EL-2/TVE-4 evidence."""
    from harness.model_migration import (
        base_arm_from_gap,
        candidate_arm_from_gap,
        validate_migration_report,
    )

    parameters = criterion.get("parameters", {})
    ref = parameters.get("report_ref")
    expected_sha256 = parameters.get("report_sha256")
    body = services.object_body(ref) if isinstance(ref, str) else None
    if body is None or hashlib.sha256(body).hexdigest() != expected_sha256:
        return VerificationResult("failed", {}, "migration_report_hash_mismatch")
    try:
        report = validate_migration_report(json.loads(body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "migration_report_invalid")
    if report["tenant_id"] != task.get("tenant_id"):
        return VerificationResult("failed", {}, "migration_report_tenant_mismatch")
    report_version = int(report["schema_version"].rsplit("v", 1)[1])

    source = report["learning_source"]
    source_body = services.object_body(source["ref"])
    try:
        source_matches = (
            source_body is not None
            and hashlib.sha256(source_body).hexdigest() == source["sha256"]
            and json.loads(source_body) == source["value"]
        )
    except (TypeError, json.JSONDecodeError):
        source_matches = False
    if not source_matches:
        return VerificationResult("failed", {}, "migration_learning_source_mismatch")
    source_verifier = (
        _compile_decision if source["kind"] == "compile_decision" else _compile_manifest
    )
    source_parameters = (
        {"decision_ref": source["ref"], "decision_sha256": source["sha256"]}
        if source["kind"] == "compile_decision"
        else {
            "compile_manifest_ref": source["ref"],
            "compile_manifest_sha256": source["sha256"],
        }
    )
    if source_verifier({"parameters": source_parameters}, task, {}, services).status != "passed":
        return VerificationResult("failed", {}, "migration_learning_source_unverified")

    base = next(item for item in report["arms"] if item["name"] == "base")
    gap_body = services.object_body(base["evidence"]["ref"])
    if gap_body is None or hashlib.sha256(gap_body).hexdigest() != base["evidence"]["sha256"]:
        return VerificationResult("failed", {}, "migration_base_evidence_mismatch")
    try:
        gap = json.loads(gap_body)
        gap_verified = _gap_report(
            {
                "parameters": {
                    "report_ref": base["evidence"]["ref"],
                    "report_sha256": base["evidence"]["sha256"],
                    "generation_policy_sha256": gap["generation_policy_sha256"],
                    "verifier_contract_digest": gap["verifier"]["contract_digest"],
                }
            },
            task,
            {},
            services,
        )
        if gap_verified.status != "passed":
            raise ValueError("gap_unverified")
        transcript_refs = {
            outcome["transcript_ref"]
            for item in gap["tasks"]
            for outcome in item["outcomes"]
            if outcome["target_fingerprint_sha256"] == base["fingerprint_sha256"]
        }
        transcripts = {
            transcript_ref: json.loads(services.object_body(transcript_ref))
            for transcript_ref in transcript_refs
        }
        rebuilt = base_arm_from_gap(
            gap,
            base["fingerprint_sha256"],
            transcripts,
            gap_report_ref=base["evidence"]["ref"],
            gap_report_sha256=base["evidence"]["sha256"],
            report_version=report_version,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "migration_base_evidence_invalid")
    if rebuilt != base:
        return VerificationResult("failed", {}, "migration_base_not_reproducible")
    if len(report["arms"]) == 1:
        return VerificationResult("passed", report["decision"])
    if len(report["arms"]) != 2:
        return VerificationResult("failed", {}, "migration_candidate_evidence_unverified")
    candidate = next((item for item in report["arms"] if item["name"] == "gap_sft"), None)
    if candidate is None or candidate["evidence"] != base["evidence"]:
        return VerificationResult("failed", {}, "migration_training_cost_unverified")
    candidate_gap_body = services.object_body(candidate["evidence"]["ref"])
    try:
        if (
            candidate_gap_body is None
            or hashlib.sha256(candidate_gap_body).hexdigest() != candidate["evidence"]["sha256"]
        ):
            raise ValueError("gap_missing")
        candidate_gap = json.loads(candidate_gap_body)
        candidate_target = next(
            item
            for item in candidate_gap["targets"]
            if item["fingerprint_sha256"] == candidate["fingerprint_sha256"]
        )
        transcript_refs = {
            outcome["transcript_ref"]
            for item in candidate_gap["tasks"]
            for outcome in item["outcomes"]
            if outcome["target_fingerprint_sha256"] == candidate["fingerprint_sha256"]
        }
        candidate_transcripts = {
            ref: json.loads(services.object_body(ref)) for ref in transcript_refs
        }
        adapter = services.adapter(candidate["subject_ref"])
        receipt_descriptor = None
        if report_version == 1:
            if candidate["metrics"]["training_cost"] is not None:
                raise ValueError("cost_unverified")
        else:
            stored_descriptor = (adapter or {}).get("config_json", {}).get("training_cost_receipt")
            receipt_body = services.object_body((stored_descriptor or {}).get("ref"))
            if (
                not isinstance(stored_descriptor, dict)
                or set(stored_descriptor) != {"ref", "sha256"}
                or receipt_body is None
                or hashlib.sha256(receipt_body).hexdigest() != stored_descriptor["sha256"]
            ):
                raise ValueError("cost_unverified")
            receipt_descriptor = {
                **stored_descriptor,
                "value": json.loads(receipt_body),
            }
        rebuilt_candidate = candidate_arm_from_gap(
            candidate_gap,
            candidate["fingerprint_sha256"],
            candidate_transcripts,
            gap_report_ref=candidate["evidence"]["ref"],
            gap_report_sha256=candidate["evidence"]["sha256"],
            adapter_id=candidate["subject_ref"],
            training_cost_receipt=receipt_descriptor,
            report_version=report_version,
        )
        snapshot = services.snapshot(adapter["snapshot_id"]) if adapter else None
        if (
            rebuilt_candidate != candidate
            or adapter is None
            or adapter["state"] not in {"candidate", "verified"}
            or adapter["artifact_sha256"] != candidate_target["fingerprint"].get("adapter_sha256")
            or snapshot is None
            or source["kind"] != "compile_manifest"
            or snapshot["compile_manifest_key"] != source["ref"]
            or snapshot["compile_manifest_sha256"] != source["sha256"]
            or (
                report_version == 2
                and (
                    receipt_descriptor["value"]["snapshot_id"] != str(adapter["snapshot_id"])
                    or receipt_descriptor["value"]["base_model_digest"]
                    != adapter["base_model_digest"]
                    or receipt_descriptor["value"]["artifact_sha256"] != adapter["artifact_sha256"]
                    or receipt_descriptor["value"]["dataset_sha256"] != snapshot["dataset_sha256"]
                )
            )
        ):
            raise ValueError("candidate_mismatch")
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "migration_candidate_evidence_invalid")
    return VerificationResult("passed", report["decision"])


def _dpo_gate(
    criterion: dict[str, Any],
    task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    """Verify that DPO remains disabled when its upstream evidence is insufficient."""
    from harness.model_migration import (
        build_dpo_gate_decision,
        validate_dpo_gate_decision,
        validate_migration_report,
    )

    parameters = criterion.get("parameters", {})
    ref = parameters.get("decision_ref")
    expected_sha256 = parameters.get("decision_sha256")
    body = services.object_body(ref) if isinstance(ref, str) else None
    if body is None or hashlib.sha256(body).hexdigest() != expected_sha256:
        return VerificationResult("failed", {}, "dpo_gate_hash_mismatch")
    try:
        decision = validate_dpo_gate_decision(json.loads(body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "dpo_gate_invalid")
    if decision["tenant_id"] != task.get("tenant_id"):
        return VerificationResult("failed", {}, "dpo_gate_tenant_mismatch")

    migration_source = decision["migration_report"]
    migration_body = services.object_body(migration_source["ref"])
    if (
        migration_body is None
        or hashlib.sha256(migration_body).hexdigest() != migration_source["sha256"]
    ):
        return VerificationResult("failed", {}, "dpo_gate_migration_hash_mismatch")
    migration_verified = _model_migration(
        {
            "parameters": {
                "report_ref": migration_source["ref"],
                "report_sha256": migration_source["sha256"],
            }
        },
        task,
        {},
        services,
    )
    if migration_verified.status != "passed":
        return VerificationResult("failed", {}, "dpo_gate_migration_unverified")
    try:
        migration = validate_migration_report(json.loads(migration_body))
        rebuilt = build_dpo_gate_decision(
            tenant_id=decision["tenant_id"],
            migration_report=migration,
            migration_report_ref=migration_source["ref"],
            migration_report_sha256=migration_source["sha256"],
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "dpo_gate_not_reproducible")
    if rebuilt != decision:
        return VerificationResult("failed", {}, "dpo_gate_not_reproducible")
    return VerificationResult("passed", decision["decision"])


def _rl_gate(
    criterion: dict[str, Any],
    task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    """Verify that RL and Agent Lightning remain disabled without prerequisite evidence."""
    from harness.model_migration import (
        build_rl_gate_decision,
        validate_dpo_gate_decision,
        validate_rl_gate_decision,
    )

    parameters = criterion.get("parameters", {})
    ref = parameters.get("decision_ref")
    expected_sha256 = parameters.get("decision_sha256")
    body = services.object_body(ref) if isinstance(ref, str) else None
    if body is None or hashlib.sha256(body).hexdigest() != expected_sha256:
        return VerificationResult("failed", {}, "rl_gate_hash_mismatch")
    try:
        decision = validate_rl_gate_decision(json.loads(body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "rl_gate_invalid")
    if decision["tenant_id"] != task.get("tenant_id"):
        return VerificationResult("failed", {}, "rl_gate_tenant_mismatch")

    dpo_source = decision["dpo_gate_decision"]
    dpo_body = services.object_body(dpo_source["ref"])
    if dpo_body is None or hashlib.sha256(dpo_body).hexdigest() != dpo_source["sha256"]:
        return VerificationResult("failed", {}, "rl_gate_dpo_hash_mismatch")
    dpo_verified = _dpo_gate(
        {
            "parameters": {
                "decision_ref": dpo_source["ref"],
                "decision_sha256": dpo_source["sha256"],
            }
        },
        task,
        {},
        services,
    )
    if dpo_verified.status != "passed":
        return VerificationResult("failed", {}, "rl_gate_dpo_unverified")
    try:
        dpo = validate_dpo_gate_decision(json.loads(dpo_body))
        rebuilt = build_rl_gate_decision(
            tenant_id=decision["tenant_id"],
            dpo_gate_decision=dpo,
            dpo_gate_decision_ref=dpo_source["ref"],
            dpo_gate_decision_sha256=dpo_source["sha256"],
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return VerificationResult("failed", {}, "rl_gate_not_reproducible")
    if rebuilt != decision:
        return VerificationResult("failed", {}, "rl_gate_not_reproducible")
    return VerificationResult(
        "passed", {**decision["decision"], "agent_lightning": decision["agent_lightning"]}
    )


def _training_snapshot(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    snapshot_id = criterion.get("parameters", {}).get("snapshot_id")
    snapshot = services.snapshot(snapshot_id) if isinstance(snapshot_id, str) else None
    if snapshot is None or snapshot["state"] != "approved":
        return VerificationResult("failed", {}, "snapshot_not_approved")
    items = snapshot.get("items", [])
    if not items or not all(item["training_allowed"] for item in items):
        return VerificationResult("failed", {}, "snapshot_training_permission_missing")
    if {item["split"] for item in items} != {"train", "validation"}:
        return VerificationResult("failed", {}, "snapshot_split_invalid")
    if any(item["source_tenant_id"] != snapshot["tenant_id"] for item in items):
        return VerificationResult("failed", {}, "snapshot_source_tenant_mismatch")
    return VerificationResult(
        "passed", {"snapshot_id": str(snapshot["snapshot_id"]), "items": len(items)}
    )


def _base_evaluation(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    evaluation_id = criterion.get("parameters", {}).get("evaluation_id")
    evaluation = services.evaluation(evaluation_id) if isinstance(evaluation_id, str) else None
    if (
        evaluation is None
        or evaluation["subject_type"] != "base"
        or evaluation["state"] != "passed"
    ):
        return VerificationResult("failed", {}, "base_evaluation_not_passed")
    gates = evaluation.get("hard_gates", {})
    if gates.get("passed") is not True or gates.get("invalidated_trials", 0):
        return VerificationResult("failed", {}, "base_evaluation_gate_failed")
    return VerificationResult("passed", {"evaluation_id": str(evaluation["evaluation_id"])})


def _training_input(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    parameters = criterion.get("parameters", {})
    snapshot = services.snapshot(parameters.get("snapshot_id"))
    base = services.evaluation(parameters.get("base_evaluation_id"))
    if snapshot is None or snapshot["state"] != "approved":
        return VerificationResult("failed", {}, "training_snapshot_not_ready")
    if base is None or base["state"] != "passed" or base["subject_type"] != "base":
        return VerificationResult("failed", {}, "training_base_evaluation_missing")
    if snapshot["base_model_digest"] != parameters.get("base_model_digest"):
        return VerificationResult("failed", {}, "training_base_model_mismatch")
    if snapshot.get("algorithm") == "sft":
        compiled = _compile_manifest(criterion, _task, _result, services)
        if compiled.status != "passed":
            return compiled
    return VerificationResult(
        "passed",
        {
            "snapshot_id": str(snapshot["snapshot_id"]),
            "base_evaluation_id": str(base["evaluation_id"]),
        },
    )


def _adapter(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    adapter_id = criterion.get("parameters", {}).get("adapter_id")
    adapter = services.adapter(adapter_id) if isinstance(adapter_id, str) else None
    if adapter is None or adapter["state"] != "verified":
        return VerificationResult("failed", {}, "adapter_not_verified")
    if adapter["safety_scan_json"].get("passed") is not True:
        return VerificationResult("failed", {}, "adapter_safety_scan_failed")
    config = adapter["config_json"]
    if config.get("format") not in {"safetensors", "safetensors+json"}:
        return VerificationResult("failed", {}, "adapter_format_not_allowed")
    return VerificationResult("passed", {"adapter_id": str(adapter["adapter_id"])})


def _evaluation(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    evaluation_id = criterion.get("parameters", {}).get("evaluation_id")
    evaluation = services.evaluation(evaluation_id) if isinstance(evaluation_id, str) else None
    if evaluation is None or evaluation["state"] != "passed":
        return VerificationResult("failed", {}, "evaluation_not_passed")
    gates = evaluation.get("hard_gates", {})
    if gates.get("passed") is not True or gates.get("invalidated_trials", 0):
        return VerificationResult("failed", {}, "evaluation_hard_gate_failed")
    if gates.get("judge_only") is True:
        return VerificationResult("failed", {}, "judge_cannot_be_release_gate")
    return VerificationResult("passed", {"evaluation_id": str(evaluation["evaluation_id"])})


def _release_v2(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    release_id = criterion.get("parameters", {}).get("release_id")
    row = services.release(release_id) if isinstance(release_id, str) else None
    manifest = row["manifest_json"] if row else {}
    required = {"adapter_id", "evaluation_id", "training_snapshot_id", "rollback_to", "guardrails"}
    if row is None or row["status"] not in {"candidate", "shadow", "canary", "promoted"}:
        return VerificationResult("failed", {}, "release_not_active")
    if not required <= manifest.keys() or manifest.get("evaluation", {}).get("passed") is not True:
        return VerificationResult("failed", {}, "release_manifest_incomplete")
    if row.get("release_scope") != "single_tenant_lora":
        return VerificationResult("failed", {}, "release_scope_unsupported")
    return VerificationResult(
        "passed", {"release_id": str(row["release_id"]), "status": row["status"]}
    )


def _qualification(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    _result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    qualification_id = criterion.get("parameters", {}).get("qualification_id")
    expected_state = criterion.get("parameters", {}).get("expected_state", "calibrated")
    if not isinstance(qualification_id, str) or expected_state not in {
        "data_approved",
        "calibrated",
        "pilot_ready",
    }:
        return VerificationResult("failed", {}, "qualification_parameters_invalid")
    row = services.qualification(qualification_id)
    if row is None:
        return VerificationResult("failed", {}, "qualification_not_found")
    if row["state"] != expected_state:
        return VerificationResult("failed", {"state": row["state"]}, "qualification_state_mismatch")
    if (
        not row["source_manifest_key"]
        or not row["source_acl_digest"]
        or not row["permission_version"]
    ):
        return VerificationResult("failed", {}, "qualification_provenance_missing")
    for key in ("source_manifest_sha256", "suite_sha256"):
        value = row[key]
        if not isinstance(value, str) or len(value) != 64:
            return VerificationResult("failed", {}, f"qualification_{key}_invalid")
    if expected_state in {"calibrated", "pilot_ready"}:
        if (
            not row["reviewer"]
            or row["reviewer"] == row["created_by"]
            or not row["base_evaluation_id"]
            or not row["candidate_evaluation_id"]
            or not row["calibration_report_key"]
            or not row["calibration_report_sha256"]
        ):
            return VerificationResult("failed", {}, "qualification_calibration_incomplete")
    if expected_state == "pilot_ready":
        if (
            not row["stable_release_id"]
            or not row["candidate_release_id"]
            or not row["deployment_evidence_key"]
            or not row["deployment_evidence_sha256"]
        ):
            return VerificationResult("failed", {}, "qualification_deployment_incomplete")
    return VerificationResult(
        "passed", {"qualification_id": qualification_id, "state": row["state"]}
    )


def _deployment_binding(
    criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    release_id = criterion.get("parameters", {}).get("release_id")
    row = services.release(release_id) if isinstance(release_id, str) else None
    try:
        binding = DeploymentBinding.from_manifest(row["manifest_json"] if row else {})
    except (TypeError, ValueError) as error:
        return VerificationResult("failed", {}, str(error))
    if row["status"] not in {"shadow", "canary", "promoted"}:
        return VerificationResult("failed", {}, "deployment_release_not_active")
    if result.get("output", {}).get("candidate_release_id") != binding.candidate_release_id:
        return VerificationResult("failed", {}, "deployment_candidate_mismatch")
    return VerificationResult(
        "passed", {"mode": binding.mode, "canary_percent": binding.canary_percent}
    )


def _shadow(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    _services: ReadOnlyServices,
) -> VerificationResult:
    try:
        validate_shadow_output(result.get("output", {}))
    except ValueError as error:
        return VerificationResult("failed", {}, str(error))
    return VerificationResult("passed", {"authority": "stable"})


def _rough_clean(
    _criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    step_id = result.get("step_id") or task["plan"][task["current_step"]]["step_id"]
    job = services.job(task["task_id"], step_id)
    artifact = next(
        (
            item
            for item in result.get("artifacts", [])
            if item.get("store") == "minio" and item.get("kind") == "cleaned_corpus"
        ),
        None,
    )
    if job is None or job["state"] != "succeeded" or not job["result_sha256"]:
        return VerificationResult("failed", {}, "job_result_unverified")
    if (
        artifact is None
        or not isinstance(artifact.get("sha256"), str)
        or len(artifact["sha256"]) != 64
    ):
        return VerificationResult("failed", {}, "cleaned_corpus_missing")
    if result.get("observed_scope") != [f"raw:{job['input_key']}"]:
        return VerificationResult("failed", {}, "job_scope_mismatch")
    return VerificationResult("passed", {"job_result_sha256": job["result_sha256"]})


def _rough_clean_v2(
    criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    outcome = _rough_clean(criterion, task, result, services)
    if outcome.status != "passed":
        return outcome
    artifact = next(
        (item for item in result.get("artifacts", []) if item.get("kind") == "cleaned_corpus"), None
    )
    if artifact is None:
        return VerificationResult("failed", {}, "cleaned_corpus_missing")
    # Spark writes one output prefix containing several products; rough-clean
    # schema verification must read only the cleaned-corpus product, not the
    # later RAG rows whose shape is intentionally different.
    records = services.object_records(artifact["id"].rstrip("/") + "/cleaned_corpus.jsonl")
    if not records:
        return VerificationResult("failed", {}, "rough_records_missing")
    accepted = 0
    for record in records:
        required = {
            "text",
            "source_uri",
            "source_version",
            "tenant_id",
            "acl_digest",
            "trust_label",
            "decision",
        }
        if not required <= record.keys() or record["tenant_id"] != task["tenant_id"]:
            return VerificationResult("failed", {}, "rough_schema_invalid")
        if record["decision"] == "accepted":
            accepted += 1
    if accepted < 1:
        return VerificationResult("failed", {}, "rough_no_accepted_records")
    return VerificationResult("passed", {"records": len(records), "accepted": accepted})


def _refined_corpus(
    _criterion: dict[str, Any],
    task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    artifact = next(
        (
            item
            for item in result.get("artifacts", [])
            if item.get("kind") == "normalized_documents"
        ),
        None,
    )
    if artifact is None:
        return VerificationResult("failed", {}, "normalized_artifact_missing")
    body = services.object_body(artifact["id"])
    if body is None or hashlib.sha256(body).hexdigest() != artifact["sha256"]:
        return VerificationResult("failed", {}, "normalized_artifact_hash_mismatch")
    try:
        normalized = json.loads(body)
    except json.JSONDecodeError:
        return VerificationResult("failed", {}, "normalized_schema_invalid")
    if normalized.get("tenant_id") != task["tenant_id"] or not normalized.get("documents"):
        return VerificationResult("failed", {}, "normalized_schema_invalid")
    for document in normalized["documents"]:
        if not document.get("acl_digest") or document.get("trust_label") != "untrusted_external":
            return VerificationResult("failed", {}, "normalized_lineage_missing")
        if not document.get("chunks") or any(not chunk.get("text") for chunk in document["chunks"]):
            return VerificationResult("failed", {}, "normalized_chunks_empty")
    return VerificationResult("passed", normalized.get("metrics", {}))


def _conflict_report(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    artifact = next(
        (item for item in result.get("artifacts", []) if item.get("kind") == "conflict_report"),
        None,
    )
    if artifact is None:
        return VerificationResult("failed", {}, "conflict_report_missing")
    report = services.object_json(artifact["id"])
    if not isinstance(report, dict) or not report.get("candidates") or "decision" not in report:
        return VerificationResult("failed", {}, "source_evidence_missing")
    if any(
        not {"source_uri", "source_version", "acl_digest", "candidate_id"} <= candidate.keys()
        for candidate in report["candidates"]
    ):
        return VerificationResult("failed", {}, "source_evidence_missing")
    return VerificationResult(
        "passed",
        {"status": report["decision"].get("status"), "candidates": len(report["candidates"])},
    )


def _conflict_decision(
    _criterion: dict[str, Any],
    _task: dict[str, Any],
    result: dict[str, Any],
    services: ReadOnlyServices,
) -> VerificationResult:
    artifact = next(
        (item for item in result.get("artifacts", []) if item.get("kind") == "conflict_decision"),
        None,
    )
    if artifact is None:
        return VerificationResult("failed", {}, "conflict_decision_missing")
    decision = services.object_json(artifact["id"])
    if (
        not isinstance(decision, dict)
        or decision.get("decision", {}).get("status") != "resolved"
        or not decision["decision"].get("approved_by")
    ):
        return VerificationResult("failed", {}, "decision_unapproved")
    return VerificationResult(
        "passed", {"selected_candidate_id": decision["decision"].get("selected_candidate_id")}
    )


def default_verifiers() -> VerifierRegistry:
    registry = VerifierRegistry()
    registry.register(VerifierSpec("verify_task_bundle", 1, _task_bundle))
    registry.register(VerifierSpec("verify_environment", 1, _environment))
    registry.register(VerifierSpec("verify_task_run", 1, _task_run))
    registry.register(VerifierSpec("verify_rag_outcome", 1, _rag_outcome))
    registry.register(VerifierSpec("verify_trial_transcript", 1, _trial_transcript))
    registry.register(VerifierSpec("verify_gap_report", 1, _gap_report))
    registry.register(VerifierSpec("verify_release_decision", 1, _release_decision))
    registry.register(VerifierSpec("verify_experience_bundle", 1, _experience_bundle))
    registry.register(VerifierSpec("verify_compile_manifest", 1, _compile_manifest))
    registry.register(VerifierSpec("verify_compile_decision", 1, _compile_decision))
    registry.register(VerifierSpec("verify_model_migration", 1, _model_migration))
    registry.register(VerifierSpec("verify_dpo_gate", 1, _dpo_gate))
    registry.register(VerifierSpec("verify_rl_gate", 1, _rl_gate))
    registry.register(VerifierSpec("verify_ingest", 1, _ingest))
    registry.register(VerifierSpec("verify_ingest", 2, _ingest_v2))
    registry.register(VerifierSpec("verify_retrieval", 1, _retrieval))
    registry.register(VerifierSpec("verify_retrieval", 2, _retrieval_v2))
    registry.register(VerifierSpec("verify_memory", 1, _memory))
    registry.register(VerifierSpec("verify_context_snapshot", 1, _context_snapshot))
    registry.register(VerifierSpec("verify_chat_capture", 1, _chat_capture))
    registry.register(VerifierSpec("verify_context_checkpoint", 1, _context_checkpoint))
    registry.register(VerifierSpec("verify_memory_distillation", 1, _memory_distillation))
    registry.register(VerifierSpec("verify_memory_policy", 1, _memory_policy))
    registry.register(VerifierSpec("verify_release", 1, _release))
    registry.register(VerifierSpec("verify_trajectory", 1, _trajectory))
    registry.register(VerifierSpec("verify_training_snapshot", 1, _training_snapshot))
    registry.register(VerifierSpec("verify_base_evaluation", 1, _base_evaluation))
    registry.register(VerifierSpec("verify_training_input", 1, _training_input))
    registry.register(VerifierSpec("verify_adapter", 1, _adapter))
    registry.register(VerifierSpec("verify_evaluation", 1, _evaluation))
    registry.register(VerifierSpec("verify_release", 2, _release_v2))
    registry.register(VerifierSpec("verify_qualification", 1, _qualification))
    registry.register(VerifierSpec("verify_deployment_binding", 1, _deployment_binding))
    registry.register(VerifierSpec("verify_shadow", 1, _shadow))
    registry.register(VerifierSpec("verify_rough_clean", 1, _rough_clean))
    registry.register(VerifierSpec("verify_rough_clean", 2, _rough_clean_v2))
    registry.register(VerifierSpec("verify_input_manifest", 1, _input_manifest))
    registry.register(VerifierSpec("verify_refined_corpus", 1, _refined_corpus))
    registry.register(VerifierSpec("verify_conflict_report", 1, _conflict_report))
    registry.register(VerifierSpec("verify_conflict_decision", 1, _conflict_decision))
    return registry
