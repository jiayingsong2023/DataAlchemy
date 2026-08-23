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
                    "fingerprint_json, outcome_json, metrics_json, failure_code FROM trajectory_trials "
                    "WHERE trial_id = %s",
                    (trial_id,),
                )
                row = cursor.fetchone()
        if row:
            row["fingerprint"] = row.pop("fingerprint_json")
            row["outcome"] = row.pop("outcome_json")
            row["metrics"] = row.pop("metrics_json")
        return row

    def snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.database.transaction(self.identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT snapshot_id, tenant_id, state, dataset_key, dataset_sha256, dataset_size, "
                    "policy_version, split_json, base_model_digest, created_by, approved_by "
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
            or (expected_source_uri is not None and citation.get("source_uri") != expected_source_uri)
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
    registry.register(VerifierSpec("verify_ingest", 1, _ingest))
    registry.register(VerifierSpec("verify_ingest", 2, _ingest_v2))
    registry.register(VerifierSpec("verify_retrieval", 1, _retrieval))
    registry.register(VerifierSpec("verify_retrieval", 2, _retrieval_v2))
    registry.register(VerifierSpec("verify_memory", 1, _memory))
    registry.register(VerifierSpec("verify_context_snapshot", 1, _context_snapshot))
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
