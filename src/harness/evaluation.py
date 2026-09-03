"""H5 evaluation, annotation and training-snapshot governance.

The service owns only the durable indexes and gates. Large transcript/dataset
objects remain in MinIO and are referenced by immutable hashes.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Iterable

from core.evidence import EvidenceObjectStore, canonical_bytes, sha256
from storage.audit import AuditLog
from storage.postgres import PostgresDatabase

from .experience import _put_immutable, validate_task_bundle_fingerprint


def _sha256(value: bytes | str | dict[str, Any]) -> str:
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _identity(tenant_id: str, username: str, role: str = "admin") -> dict[str, str]:
    return {"tenant_id": tenant_id, "username": username, "role": role}


def _source_filter(selector: dict[str, str], alias: str = "a") -> tuple[str, Any]:
    values = {key: value for key, value in selector.items() if value}
    if len(values) != 1:
        raise ValueError("source_selector_invalid")
    key, value = next(iter(values.items()))
    if key == "source_acl_digest":
        return f"{alias}.source_acl_digest = %s", value
    if key == "permission_version":
        return f"{alias}.training_permission_version = %s", value
    if key == "source_version":
        return (
            f"{alias}.label_json @> %s::jsonb",
            json.dumps({"evidence_refs": [{"source_version": value}]}),
        )
    raise ValueError("source_selector_invalid")


def validate_suite_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a fixed evaluation suite manifest."""
    if not isinstance(manifest, dict) or not manifest.get("version"):
        raise ValueError("suite_version_missing")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("suite_cases_missing")
    seen: set[str] = set()
    normalized = []
    for case in cases:
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("case_id"), str)
            or not case["case_id"]
        ):
            raise ValueError("suite_case_invalid")
        case_id = case["case_id"]
        if case_id in seen:
            raise ValueError("suite_case_duplicate")
        seen.add(case_id)
        input_sha256 = case.get("input_sha256")
        if input_sha256 is None:
            if not isinstance(case.get("query"), str) or not case["query"]:
                raise ValueError("suite_input_hash_missing")
            input_sha256 = _sha256(case["query"])
        elif (
            not isinstance(input_sha256, str)
            or len(input_sha256) != 64
            or any(character not in "0123456789abcdef" for character in input_sha256)
        ):
            raise ValueError("suite_input_hash_invalid")
        normalized.append({**case, "case_id": case_id, "input_sha256": input_sha256})
    result = {
        "version": str(manifest["version"]),
        "policy_version": str(manifest.get("policy_version", "h5-default-1")),
        "cases": normalized,
    }
    source = manifest.get("source")
    if source is not None:
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("sha256"), str)
            or len(source["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in source["sha256"])
        ):
            raise ValueError("suite_source_invalid")
        result["source"] = dict(source)
    return result


def validate_trial_result(result: dict[str, Any], *, required_valid: bool = True) -> str:
    """Return a valid terminal trial state, failing closed on ambiguous outcomes."""
    if not isinstance(result, dict):
        raise ValueError("trial_result_invalid")
    state = result.get("state")
    if state not in {"succeeded", "failed", "invalidated", "aborted"}:
        raise ValueError("trial_state_invalid")
    if state == "invalidated" and required_valid and not result.get("invalid_reason"):
        raise ValueError("invalidated_reason_missing")
    if state == "failed" and not result.get("failure_code"):
        raise ValueError("failure_code_missing")
    return state


def validate_model_fingerprint(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the model-dependent identity attached to one rollout."""
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "model_id",
        "model_sha256",
        "tokenizer_sha256",
        "chat_template_sha256",
        "adapter_sha256",
    }:
        raise ValueError("model_fingerprint_invalid")
    if (
        value["schema_version"] != "model_fingerprint.v1"
        or not isinstance(value["model_id"], str)
        or not value["model_id"]
    ):
        raise ValueError("model_fingerprint_invalid")
    for key in ("model_sha256", "tokenizer_sha256", "chat_template_sha256"):
        digest = value[key]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"model_fingerprint_{key}_invalid")
    adapter = value["adapter_sha256"]
    if adapter is not None and (
        not isinstance(adapter, str)
        or len(adapter) != 64
        or any(character not in "0123456789abcdef" for character in adapter)
    ):
        raise ValueError("model_fingerprint_adapter_sha256_invalid")
    return dict(value)


def model_fingerprint_digest(value: dict[str, Any]) -> str:
    return _sha256(validate_model_fingerprint(value))


def _tree_digest(root: Path, patterns: tuple[str, ...]) -> str:
    files = sorted({item for pattern in patterns for item in root.glob(pattern) if item.is_file()})
    if not files:
        raise ValueError(f"model_fingerprint_files_missing:{root}")
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.relative_to(root).as_posix().encode())
        with item.open("rb") as handle:
            for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def model_path_fingerprint(
    model_path: str | Path,
    *,
    model_root: str | Path,
    adapter_path: str | Path | None = None,
) -> dict[str, Any]:
    """Fingerprint one allowlisted local model and its tokenizer/template."""
    root = Path(model_root).resolve()
    model = Path(model_path).resolve()
    adapter = Path(adapter_path).resolve() if adapter_path else None
    if not model.is_relative_to(root) or not model.is_dir():
        raise ValueError("model_fingerprint_outside_root")
    if adapter and (not adapter.is_relative_to(root) or not adapter.is_dir()):
        raise ValueError("adapter_fingerprint_outside_root")
    tokenizer_config = model / "tokenizer_config.json"
    weight_patterns = (
        ("*.safetensors",) if any(model.glob("*.safetensors")) else ("pytorch_model*.bin",)
    )
    return validate_model_fingerprint(
        {
            "schema_version": "model_fingerprint.v1",
            "model_id": str(model),
            "model_sha256": _tree_digest(model, weight_patterns),
            "tokenizer_sha256": _tree_digest(
                model,
                ("tokenizer.json", "tokenizer.model", "vocab.json", "vocab.txt", "merges.txt"),
            ),
            "chat_template_sha256": _sha256(
                tokenizer_config.read_bytes() if tokenizer_config.is_file() else b""
            ),
            "adapter_sha256": _tree_digest(adapter, ("*",)) if adapter else None,
        }
    )


def validate_trial_transcript(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the immutable, post-model evidence for one trial."""
    required = {
        "schema_version",
        "trial_id",
        "case_id",
        "task_bundle_id",
        "environment_receipt_ref",
        "environment_receipt_sha256",
        "prompt",
        "answer",
        "status",
        "citations",
        "latency_ms",
        "model_fingerprint",
        "generation_policy",
        "generation_policy_sha256",
        "verifier",
    }
    if not isinstance(value, dict) or not required <= value.keys():
        raise ValueError("trial_transcript_incomplete")
    if value["schema_version"] != "trial_transcript.v1":
        raise ValueError("trial_transcript_schema_invalid")
    for key in ("trial_id", "case_id", "task_bundle_id", "prompt"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"trial_transcript_{key}_invalid")
    if not isinstance(value["answer"], str):
        raise ValueError("trial_transcript_answer_invalid")
    if not value["task_bundle_id"].startswith("sha256:"):
        raise ValueError("trial_transcript_task_bundle_id_invalid")
    for key in ("environment_receipt_sha256", "generation_policy_sha256"):
        if not isinstance(value[key], str) or len(value[key]) != 64:
            raise ValueError(f"trial_transcript_{key}_invalid")
    if (
        not isinstance(value["generation_policy"], dict)
        or _sha256(value["generation_policy"]) != value["generation_policy_sha256"]
    ):
        raise ValueError("trial_transcript_generation_policy_invalid")
    if (
        not isinstance(value["environment_receipt_ref"], str)
        or not value["environment_receipt_ref"]
    ):
        raise ValueError("trial_transcript_environment_receipt_ref_invalid")
    if value["status"] not in {"grounded", "abstained", "generated"}:
        raise ValueError("trial_transcript_status_invalid")
    if (
        not isinstance(value["citations"], list)
        or not isinstance(value["latency_ms"], (int, float))
        or value["latency_ms"] < 0
    ):
        raise ValueError("trial_transcript_output_invalid")
    validate_model_fingerprint(value["model_fingerprint"])
    verifier = value["verifier"]
    if (
        not isinstance(verifier, dict)
        or verifier.get("name") != "verify_rag_outcome"
        or verifier.get("version") != 1
        or verifier.get("status") not in {"passed", "failed", "blocked"}
        or not isinstance(verifier.get("contract_digest"), str)
        or len(verifier["contract_digest"]) != 64
    ):
        raise ValueError("trial_transcript_verifier_invalid")
    return dict(value)


def build_gap_report(
    target_fingerprints: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    *,
    generation_policy_sha256: str,
    verifier_contract_digest: str,
) -> dict[str, Any]:
    """Build the smallest deterministic two-target capability-gap report."""
    if len(target_fingerprints) != 2:
        raise ValueError("gap_report_two_targets_required")
    target_digests = [model_fingerprint_digest(item) for item in target_fingerprints]
    if len(set(target_digests)) != 2:
        raise ValueError("gap_report_distinct_targets_required")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes:
        if outcome.get("split") not in {
            "train",
            "validation",
            "evaluation",
            "evaluation_holdout",
        }:
            raise ValueError("gap_report_split_missing")
        grouped.setdefault(outcome["task_bundle_id"], []).append(dict(outcome))
    tasks = []
    for task_bundle_id, task_outcomes in sorted(grouped.items()):
        if (
            len(task_outcomes) != 2
            or {item["target_fingerprint_sha256"] for item in task_outcomes} != set(target_digests)
            or len({item.get("case_id") for item in task_outcomes}) != 1
            or len({item.get("split") for item in task_outcomes}) != 1
            or any(
                item.get("state") not in {"succeeded", "failed", "invalidated"}
                for item in task_outcomes
            )
        ):
            raise ValueError("gap_report_target_coverage_mismatch")
        invalid = any(item["state"] == "invalidated" for item in task_outcomes)
        solved = sum(item["state"] == "succeeded" for item in task_outcomes)
        classification = (
            "invalid"
            if invalid
            else ("solved" if solved == 2 else "weak" if solved == 1 else "failed")
        )
        tasks.append(
            {
                "task_bundle_id": task_bundle_id,
                "case_id": task_outcomes[0]["case_id"],
                "split": task_outcomes[0]["split"],
                "classification": classification,
                "outcomes": sorted(
                    task_outcomes, key=lambda item: item["target_fingerprint_sha256"]
                ),
            }
        )
    valid = sum(item["classification"] != "invalid" for item in tasks)
    return {
        "schema_version": "gap_report.v1",
        "targets": [
            {"fingerprint_sha256": digest, "fingerprint": fingerprint}
            for digest, fingerprint in zip(target_digests, target_fingerprints, strict=True)
        ],
        "generation_policy_sha256": generation_policy_sha256,
        "verifier": {
            "name": "verify_rag_outcome",
            "version": 1,
            "contract_digest": verifier_contract_digest,
        },
        "tasks": tasks,
        "metrics": {
            "valid_tasks": valid,
            "invalid_tasks": len(tasks) - valid,
            "capability_denominator": valid,
        },
    }


def validate_training_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate immutable train/validation membership before DB insertion."""
    normalized: list[dict[str, Any]] = []
    item_ids: set[str] = set()
    source_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("snapshot_item_invalid")
        item_id = item.get("item_id")
        source_id = item.get("source_id")
        if not isinstance(item_id, str) or not item_id or item_id in item_ids:
            raise ValueError("snapshot_item_duplicate")
        if not isinstance(source_id, str) or not source_id or source_id in source_ids:
            raise ValueError("snapshot_source_duplicate")
        if item.get("split") not in {"train", "validation"}:
            raise ValueError("snapshot_split_invalid")
        if item.get("source_type") != "trajectory_annotation":
            raise ValueError("snapshot_source_type_invalid")
        if item.get("training_allowed") is not True:
            raise ValueError("training_permission_missing")
        if not item.get("training_purpose") or not item.get("training_permission_version"):
            raise ValueError("training_permission_metadata_missing")
        source_sha256 = item.get("source_sha256")
        if not isinstance(source_sha256, str) or len(source_sha256) != 64:
            raise ValueError("snapshot_source_hash_invalid")
        item_ids.add(item_id)
        source_ids.add(source_id)
        normalized.append(dict(item))
    if not normalized or not any(item["split"] == "train" for item in normalized):
        raise ValueError("snapshot_train_split_missing")
    if not any(item["split"] == "validation" for item in normalized):
        raise ValueError("snapshot_validation_split_missing")
    return normalized


def validate_evaluation_pair(base: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Require a candidate evaluation to be comparable with its base."""
    for key in ("suite_sha256", "policy_version", "required_trials"):
        if base.get(key) != candidate.get(key):
            raise ValueError(f"evaluation_{key}_mismatch")
    if base.get("state") != "passed":
        raise ValueError("base_evaluation_not_passed")
    if candidate.get("state") != "passed":
        raise ValueError("candidate_evaluation_not_passed")
    hard_gates = candidate.get("hard_gates", {})
    if hard_gates.get("passed") is not True:
        raise ValueError("candidate_hard_gate_failed")
    if candidate.get("invalidated_trials", 0):
        raise ValueError("candidate_has_invalidated_trials")


class EvaluationService:
    """Tenant-scoped H5 persistence and fail-closed governance operations."""

    def __init__(
        self,
        database_url: str,
        annotation_store: EvidenceObjectStore | None = None,
    ):
        self.database = PostgresDatabase(database_url)
        self.audit = AuditLog(database_url)
        self.annotation_store = annotation_store

    def create_campaign(
        self,
        identity: dict[str, str],
        suite: dict[str, Any],
        *,
        subject_type: str,
        subject_ref: str,
        required_trials: int = 3,
    ) -> str:
        if identity.get("role") not in {"admin", "reviewer"}:
            raise PermissionError("Evaluation campaign requires reviewer role")
        if required_trials < 1:
            raise ValueError("required_trials_invalid")
        suite = validate_suite_manifest(suite)
        if subject_type not in {"base", "adapter", "release"} or not subject_ref:
            raise ValueError("evaluation_subject_invalid")
        evaluation_id = str(uuid.uuid4())
        suite_sha256 = _sha256(suite)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO evaluation_campaigns "
                    "(evaluation_id, tenant_id, created_by, subject_type, subject_ref, suite_version, "
                    "suite_sha256, policy_version, required_trials, state) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft')",
                    (
                        evaluation_id,
                        identity["tenant_id"],
                        identity["username"],
                        subject_type,
                        subject_ref,
                        suite["version"],
                        suite_sha256,
                        suite["policy_version"],
                        required_trials,
                    ),
                )
        self.audit.record(
            identity, "evaluation.campaign_created", "evaluation", resource_id=evaluation_id
        )
        return evaluation_id

    def register_trial(
        self,
        identity: dict[str, str],
        evaluation_id: str,
        task: dict[str, Any],
        *,
        case_id: str,
        trial_no: int,
        fingerprint: dict[str, Any],
    ) -> str:
        if task.get("tenant_id") != identity["tenant_id"]:
            raise PermissionError("trial_tenant_mismatch")
        if trial_no < 1 or not case_id:
            raise ValueError("trial_identity_invalid")
        fingerprint = validate_task_bundle_fingerprint(fingerprint)
        trial_id = str(uuid.uuid4())
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO trajectory_trials "
                    "(trial_id, evaluation_id, run_id, task_id, tenant_id, case_id, trial_no, state, fingerprint_json) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s::jsonb)",
                    (
                        trial_id,
                        evaluation_id,
                        task["run_id"],
                        task["task_id"],
                        identity["tenant_id"],
                        case_id,
                        trial_no,
                        json.dumps(fingerprint, ensure_ascii=False),
                    ),
                )
        return trial_id

    def finish_trial(
        self,
        identity: dict[str, str],
        trial_id: str,
        result: dict[str, Any],
        *,
        transcript_key: str | None = None,
        transcript_sha256: str | None = None,
        simulation: bool = False,
    ) -> None:
        state = validate_trial_result(result)
        if simulation and result.get("metrics", {}).get("simulation") is not True:
            raise ValueError("trial_simulation_marker_missing")
        if state in {"succeeded", "failed"} and not simulation:
            validate_model_fingerprint(result.get("model_fingerprint"))
            if not transcript_key or not transcript_sha256:
                raise ValueError("valid_trial_transcript_missing")
        if transcript_sha256 is not None and (
            len(transcript_sha256) != 64
            or any(character not in "0123456789abcdef" for character in transcript_sha256)
        ):
            raise ValueError("transcript_hash_invalid")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE trajectory_trials SET state = %s, outcome_json = %s::jsonb, metrics_json = %s::jsonb, "
                    "failure_code = %s, transcript_key = %s, transcript_sha256 = %s, completed_at = now() "
                    "WHERE trial_id = %s AND tenant_id = %s AND state IN ('queued', 'running')",
                    (
                        state,
                        json.dumps(result, ensure_ascii=False),
                        json.dumps(result.get("metrics", {}), ensure_ascii=False),
                        result.get("failure_code") or result.get("invalid_reason"),
                        transcript_key,
                        transcript_sha256,
                        trial_id,
                        identity["tenant_id"],
                    ),
                )
                if cursor.rowcount != 1:
                    cursor.execute(
                        "SELECT state, transcript_key, transcript_sha256 FROM trajectory_trials "
                        "WHERE trial_id = %s AND tenant_id = %s",
                        (trial_id, identity["tenant_id"]),
                    )
                    current = cursor.fetchone()
                    if current is None or (
                        current["state"],
                        current["transcript_key"],
                        current["transcript_sha256"],
                    ) != (state, transcript_key, transcript_sha256):
                        raise ValueError("trial_not_active")

    def complete_campaign(
        self,
        identity: dict[str, str],
        evaluation_id: str,
        result: dict[str, Any],
        *,
        baseline_evaluation_id: str | None = None,
    ) -> str:
        """Persist one fixed-suite result; missing gates fail closed."""
        hard_gates = result.get("hard_gates")
        if not isinstance(hard_gates, dict) or hard_gates.get("passed") is not True:
            state = "failed"
        else:
            state = "passed"
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT suite_sha256, policy_version, required_trials, subject_type, state "
                    "FROM evaluation_campaigns WHERE evaluation_id = %s FOR UPDATE",
                    (evaluation_id,),
                )
                campaign = cursor.fetchone()
                if campaign is None:
                    raise ValueError("evaluation_not_found")
                cursor.execute(
                    "SELECT count(*) AS valid_trials FROM trajectory_trials "
                    "WHERE evaluation_id = %s AND state IN ('succeeded', 'failed')",
                    (evaluation_id,),
                )
                if int(cursor.fetchone()["valid_trials"]) < campaign["required_trials"]:
                    state = "blocked"
                if baseline_evaluation_id:
                    cursor.execute(
                        "SELECT suite_sha256, policy_version, required_trials, state "
                        "FROM evaluation_campaigns WHERE evaluation_id = %s",
                        (baseline_evaluation_id,),
                    )
                    baseline = cursor.fetchone()
                    if baseline is None or baseline["state"] != "passed":
                        raise ValueError("base_evaluation_not_passed")
                    if any(
                        campaign[key] != baseline[key]
                        for key in ("suite_sha256", "policy_version", "required_trials")
                    ):
                        raise ValueError("evaluation_baseline_mismatch")
                cursor.execute(
                    "UPDATE evaluation_campaigns SET state = %s, metrics_json = %s::jsonb, "
                    "hard_gates_json = %s::jsonb, baseline_evaluation_id = %s, completed_at = now() "
                    "WHERE evaluation_id = %s",
                    (
                        state,
                        json.dumps(result.get("metrics", {}), ensure_ascii=False),
                        json.dumps(hard_gates, ensure_ascii=False),
                        baseline_evaluation_id,
                        evaluation_id,
                    ),
                )
        self.audit.record(
            identity,
            f"evaluation.{state}",
            "evaluation",
            resource_id=evaluation_id,
            metadata={"hard_gates": hard_gates},
        )
        return state

    def create_annotation(
        self,
        identity: dict[str, str],
        *,
        run_id: str,
        trial_id: str | None,
        kind: str,
        label: dict[str, Any],
        content_key: str | None = None,
        content_sha256: str | None = None,
        source_acl_digest: str | None = None,
    ) -> str:
        if kind not in {"user_feedback", "human_review", "verifier_label"}:
            raise ValueError("annotation_kind_invalid")
        annotation_id = str(uuid.uuid4())
        status = "unrated" if kind == "user_feedback" else "candidate"
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO trajectory_annotations "
                    "(annotation_id, trial_id, run_id, tenant_id, kind, label_json, content_key, "
                    "content_sha256, source_acl_digest, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s) "
                    "ON CONFLICT (tenant_id, run_id, kind, content_key) "
                    "WHERE kind = 'user_feedback' AND content_key IS NOT NULL DO NOTHING "
                    "RETURNING annotation_id",
                    (
                        annotation_id,
                        trial_id,
                        run_id,
                        identity["tenant_id"],
                        kind,
                        json.dumps(label, ensure_ascii=False),
                        content_key,
                        content_sha256,
                        source_acl_digest,
                        status,
                    ),
                )
                row = cursor.fetchone()
                if row:
                    return str(row["annotation_id"])
                cursor.execute(
                    "SELECT annotation_id FROM trajectory_annotations "
                    "WHERE tenant_id = %s AND run_id = %s AND kind = %s AND content_key = %s",
                    (identity["tenant_id"], run_id, kind, content_key),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("annotation_create_conflict_without_existing_row")
        return str(row["annotation_id"])

    def review_annotation(
        self,
        identity: dict[str, str],
        annotation_id: str,
        *,
        status: str,
        training_allowed: bool = False,
        training_purpose: str | None = None,
        permission_version: str | None = None,
        reason: str | None = None,
        expected_response: str | None = None,
        expected_citations: list[dict[str, Any]] | None = None,
    ) -> None:
        if identity.get("role") not in {"admin", "reviewer"}:
            raise PermissionError("Annotation review requires reviewer role")
        if status not in {"approved", "rejected", "revoked"}:
            raise ValueError("annotation_status_invalid")
        if status == "approved" and (not training_purpose or not permission_version):
            raise ValueError("training_permission_metadata_missing")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT a.annotation_id, a.kind, a.label_json, a.content_key, "
                    "a.content_sha256, t.owner "
                    "FROM trajectory_annotations a "
                    "JOIN agent_tasks t ON t.run_id = a.run_id "
                    "WHERE a.annotation_id = %s FOR UPDATE OF a",
                    (annotation_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise PermissionError("Annotation not found")
                if row["owner"] == identity["username"]:
                    raise PermissionError("Creator cannot review own annotation")
                correction: dict[str, Any] = {}
                revision_key = row["content_key"]
                revision_sha256 = row["content_sha256"]
                if status == "approved" and training_allowed and row["kind"] == "user_feedback":
                    label = row["label_json"] or {}
                    source_revision = label.get("review_revision") or {
                        "source_content_key": row["content_key"],
                        "source_content_sha256": row["content_sha256"],
                    }
                    if not isinstance(source_revision, dict) or not all(source_revision.values()):
                        raise ValueError("feedback_source_artifact_missing")
                    if not label.get("evidence_refs"):
                        raise ValueError("feedback_training_evidence_missing")
                    if not expected_response or not expected_response.strip():
                        raise ValueError("feedback_expected_response_missing")
                    if not expected_citations:
                        raise ValueError("feedback_expected_citations_missing")
                    available = {
                        (span_id, evidence.get("content_sha256"))
                        for evidence in label["evidence_refs"]
                        for span_id in evidence.get("span_ids", [])
                    }
                    submitted = {
                        (span_id, citation.get("source_content_sha256"))
                        for citation in expected_citations
                        for span_id in citation.get("source_span_ids", [])
                    }
                    if not submitted or not submitted <= available:
                        raise ValueError("feedback_expected_citations_invalid")
                    correction = {
                        "expected_response": expected_response.strip(),
                        "expected_citations": expected_citations,
                        "review_revision": source_revision,
                    }
                    if self.annotation_store is None:
                        raise RuntimeError("feedback_revision_store_missing")
                    corrected_label = {**label, **correction}
                    body = canonical_bytes(corrected_label)
                    revision_sha256 = sha256(body)
                    revision_key = (
                        f"tenants/{identity['tenant_id']}/annotations/revisions/sha256/"
                        f"{revision_sha256}.json"
                    )
                    _put_immutable(self.annotation_store, revision_key, body)
                cursor.execute(
                    "UPDATE trajectory_annotations SET label_json = label_json || %s::jsonb, "
                    "content_key = %s, content_sha256 = %s, status = %s, training_allowed = %s, "
                    "training_purpose = %s, training_permission_version = %s, reviewer = %s, reason = %s, "
                    "reviewed_at = now() WHERE annotation_id = %s",
                    (
                        json.dumps(correction, ensure_ascii=False),
                        revision_key,
                        revision_sha256,
                        status,
                        training_allowed if status == "approved" else False,
                        training_purpose if status == "approved" else None,
                        permission_version if status == "approved" else None,
                        identity["username"],
                        reason,
                        annotation_id,
                    ),
                )

    def create_snapshot(
        self,
        identity: dict[str, str],
        *,
        annotation_items: Iterable[dict[str, Any]],
        dataset_key: str,
        dataset_sha256: str,
        dataset_size: int,
        base_model_digest: str,
        policy_version: str,
        compile_manifest_key: str | None = None,
        compile_manifest_sha256: str | None = None,
        target_tokenizer_digest: str | None = None,
        chat_template_digest: str | None = None,
    ) -> str:
        if identity.get("role") not in {"admin", "reviewer"}:
            raise PermissionError("Snapshot creation requires reviewer role")
        if not dataset_key or len(dataset_sha256) != 64 or dataset_size < 0:
            raise ValueError("snapshot_artifact_invalid")
        compile_values = (
            compile_manifest_key,
            compile_manifest_sha256,
            target_tokenizer_digest,
            chat_template_digest,
        )
        if not all(value is not None for value in compile_values):
            raise ValueError("snapshot_compile_manifest_required")
        if not str(compile_manifest_key).startswith(
            f"tenants/{identity['tenant_id']}/compiler/manifests/sha256/"
        ):
            raise ValueError("snapshot_compile_manifest_invalid")
        if any(
            len(str(value)) != 64
            or any(character not in "0123456789abcdef" for character in str(value))
            for value in compile_values[1:]
        ):
            raise ValueError("snapshot_compile_manifest_invalid")
        items = validate_training_items(annotation_items)
        split_json = {
            "train": sum(item["split"] == "train" for item in items),
            "validation": sum(item["split"] == "validation" for item in items),
        }
        snapshot_id = str(uuid.uuid4())
        source_ids = [item["source_id"] for item in items]
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT annotation_id, tenant_id, status, training_allowed, content_sha256, "
                    "source_acl_digest, "
                    "training_purpose, training_permission_version FROM trajectory_annotations "
                    "WHERE annotation_id = ANY(%s) FOR SHARE",
                    (source_ids,),
                )
                rows = {str(row["annotation_id"]): row for row in cursor.fetchall()}
                if len(rows) != len(source_ids):
                    raise PermissionError("snapshot_source_missing")
                for item in items:
                    row = rows[item["source_id"]]
                    if row["tenant_id"] != identity["tenant_id"]:
                        raise PermissionError("snapshot_source_tenant_mismatch")
                    if row["status"] != "approved" or row["training_allowed"] is not True:
                        raise ValueError("snapshot_source_not_approved")
                    if row["content_sha256"] != item["source_sha256"]:
                        raise ValueError("snapshot_source_hash_mismatch")
                    if row["source_acl_digest"] != item.get("source_acl_digest"):
                        raise ValueError("snapshot_source_acl_mismatch")
                    if (
                        row["training_purpose"] != item["training_purpose"]
                        or row["training_permission_version"] != item["training_permission_version"]
                    ):
                        raise ValueError("snapshot_permission_mismatch")
                cursor.execute(
                    "INSERT INTO training_snapshots "
                    "(snapshot_id, tenant_id, created_by, state, dataset_key, dataset_sha256, dataset_size, "
                    "policy_version, split_json, base_model_digest, algorithm, compile_manifest_key, "
                    "compile_manifest_sha256, target_tokenizer_digest, chat_template_digest) "
                    "VALUES (%s, %s, %s, 'candidate', %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)",
                    (
                        snapshot_id,
                        identity["tenant_id"],
                        identity["username"],
                        dataset_key,
                        dataset_sha256,
                        dataset_size,
                        policy_version,
                        json.dumps(split_json),
                        base_model_digest,
                        "sft",
                        compile_manifest_key,
                        compile_manifest_sha256,
                        target_tokenizer_digest,
                        chat_template_digest,
                    ),
                )
                for item in items:
                    cursor.execute(
                        "INSERT INTO training_snapshot_items "
                        "(snapshot_id, item_id, split, source_type, source_id, source_tenant_id, source_sha256, "
                        "source_acl_digest, training_allowed, training_purpose, training_permission_version, transform_digest) "
                        "VALUES (%s, %s, %s, 'trajectory_annotation', %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            snapshot_id,
                            item["item_id"],
                            item["split"],
                            item["source_id"],
                            identity["tenant_id"],
                            item["source_sha256"],
                            item.get("source_acl_digest"),
                            True,
                            item["training_purpose"],
                            item["training_permission_version"],
                            item.get("transform_digest"),
                        ),
                    )
        self.audit.record(
            identity, "training.snapshot_created", "training_snapshot", resource_id=snapshot_id
        )
        return snapshot_id

    def create_adapter_candidate(
        self,
        identity: dict[str, str],
        *,
        snapshot_id: str,
        base_model_digest: str,
        tokenizer_digest: str,
        artifact_key: str,
        artifact_sha256: str,
        artifact_size: int,
        config: dict[str, Any],
        environment: dict[str, Any],
        safety_scan: dict[str, Any],
        adapter_id: str | None = None,
    ) -> str:
        if identity.get("role") not in {"admin", "reviewer"}:
            raise PermissionError("Adapter creation requires reviewer role")
        if not artifact_key or len(artifact_sha256) != 64 or artifact_size < 1:
            raise ValueError("adapter_artifact_invalid")
        if not isinstance(config, dict) or config.get("format") not in {
            "safetensors",
            "safetensors+json",
        }:
            raise ValueError("adapter_format_not_allowed")
        adapter_id = adapter_id or str(uuid.uuid4())
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state, base_model_digest FROM training_snapshots WHERE snapshot_id = %s FOR SHARE",
                    (snapshot_id,),
                )
                snapshot = cursor.fetchone()
                if snapshot is None or snapshot["state"] != "approved":
                    raise ValueError("snapshot_not_approved")
                if snapshot["base_model_digest"] != base_model_digest:
                    raise ValueError("adapter_base_model_mismatch")
                cursor.execute(
                    "INSERT INTO adapter_manifests "
                    "(adapter_id, tenant_id, snapshot_id, base_model_digest, tokenizer_digest, artifact_key, "
                    "artifact_sha256, artifact_size, config_json, environment_json, safety_scan_json, state) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, 'candidate')",
                    (
                        adapter_id,
                        identity["tenant_id"],
                        snapshot_id,
                        base_model_digest,
                        tokenizer_digest,
                        artifact_key,
                        artifact_sha256,
                        artifact_size,
                        json.dumps(config),
                        json.dumps(environment),
                        json.dumps(safety_scan),
                    ),
                )
        return adapter_id

    def verify_adapter(self, identity: dict[str, str], adapter_id: str, evaluation_id: str) -> None:
        if identity.get("role") not in {"admin", "reviewer"}:
            raise PermissionError("Adapter verification requires reviewer role")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT a.state, a.safety_scan_json, s.state AS snapshot_state "
                    "FROM adapter_manifests a JOIN training_snapshots s ON s.snapshot_id = a.snapshot_id "
                    "WHERE a.adapter_id = %s FOR UPDATE",
                    (adapter_id,),
                )
                row = cursor.fetchone()
                if (
                    row is None
                    or row["state"] != "candidate"
                    or row["snapshot_state"] != "approved"
                ):
                    raise ValueError("adapter_prerequisite_failed")
                if row["safety_scan_json"].get("passed") is not True:
                    raise ValueError("adapter_safety_scan_failed")
                cursor.execute(
                    "SELECT state, subject_type FROM evaluation_campaigns WHERE evaluation_id = %s",
                    (evaluation_id,),
                )
                evaluation = cursor.fetchone()
                if (
                    evaluation is None
                    or evaluation["state"] != "passed"
                    or evaluation["subject_type"] != "adapter"
                ):
                    raise ValueError("adapter_evaluation_not_passed")
                cursor.execute(
                    "UPDATE adapter_manifests SET state = 'verified', evaluation_id = %s WHERE adapter_id = %s",
                    (evaluation_id, adapter_id),
                )

    def approve_snapshot(self, identity: dict[str, str], snapshot_id: str) -> None:
        if identity.get("role") not in {"admin", "reviewer"}:
            raise PermissionError("Snapshot approval requires reviewer role")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT snapshot_id, state, created_by FROM training_snapshots "
                    "WHERE snapshot_id = %s FOR UPDATE",
                    (snapshot_id,),
                )
                row = cursor.fetchone()
                if row is None or row["state"] != "candidate":
                    raise ValueError("snapshot_not_candidate")
                if row["created_by"] == identity["username"]:
                    raise PermissionError("Creator cannot approve own snapshot")
                cursor.execute(
                    "SELECT count(*) AS count FROM training_snapshot_items "
                    "WHERE snapshot_id = %s AND training_allowed = true",
                    (snapshot_id,),
                )
                if int(cursor.fetchone()["count"]) < 2:
                    raise ValueError("snapshot_has_too_few_items")
                cursor.execute(
                    "UPDATE training_snapshots SET state = 'approved', approved_by = %s, approved_at = now() "
                    "WHERE snapshot_id = %s",
                    (identity["username"], snapshot_id),
                )

    def revoke_snapshot(self, identity: dict[str, str], snapshot_id: str, reason: str) -> None:
        if identity.get("role") not in {"admin", "reviewer"}:
            raise PermissionError("Snapshot revoke requires reviewer role")
        if not reason:
            raise ValueError("revoke_reason_missing")
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE training_snapshots SET state = 'revoked', revoke_reason = %s "
                    "WHERE snapshot_id = %s AND state <> 'revoked'",
                    (reason, snapshot_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("snapshot_not_found")
                cursor.execute(
                    "UPDATE adapter_manifests SET state = 'revoked', revoked_at = now(), revoke_reason = %s "
                    "WHERE snapshot_id = %s AND state <> 'revoked'",
                    (reason, snapshot_id),
                )
                cursor.execute(
                    "UPDATE release_records SET status = 'rolled_back', updated_at = now(), version = version + 1 "
                    "WHERE training_snapshot_id = %s AND status IN ('candidate', 'shadow', 'canary', 'promoted')",
                    (snapshot_id,),
                )

    def source_impact(self, identity: dict[str, str], **selector: str) -> dict[str, list[str]]:
        if identity.get("role") not in {"admin", "reviewer"}:
            raise PermissionError("Source impact requires reviewer role")
        clause, value = _source_filter(selector)
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT a.annotation_id, i.snapshot_id, m.adapter_id, r.release_id "
                    "FROM trajectory_annotations a "
                    "LEFT JOIN training_snapshot_items i ON i.source_id = a.annotation_id "
                    "LEFT JOIN adapter_manifests m ON m.snapshot_id = i.snapshot_id "
                    "LEFT JOIN release_records r ON r.training_snapshot_id = i.snapshot_id "
                    f"WHERE {clause}",
                    (value,),
                )
                rows = cursor.fetchall()
        return {
            key: sorted({str(row[column]) for row in rows if row[column] is not None})
            for key, column in (
                ("annotations", "annotation_id"),
                ("snapshots", "snapshot_id"),
                ("adapters", "adapter_id"),
                ("releases", "release_id"),
            )
        }

    def revoke_source(
        self, identity: dict[str, str], *, reason: str, **selector: str
    ) -> dict[str, list[str]]:
        if identity.get("role") not in {"admin", "reviewer"}:
            raise PermissionError("Source revoke requires reviewer role")
        if not reason:
            raise ValueError("revoke_reason_missing")
        clause, value = _source_filter(selector)
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT annotation_id FROM trajectory_annotations a WHERE {clause} FOR UPDATE",
                    (value,),
                )
                annotation_ids = [str(row["annotation_id"]) for row in cursor.fetchall()]
                if not annotation_ids:
                    raise ValueError("source_impact_empty")
                cursor.execute(
                    "SELECT DISTINCT snapshot_id FROM training_snapshot_items "
                    "WHERE source_id = ANY(%s)",
                    (annotation_ids,),
                )
                snapshot_ids = [str(row["snapshot_id"]) for row in cursor.fetchall()]
                cursor.execute(
                    "UPDATE trajectory_annotations SET status = 'revoked', training_allowed = false, "
                    "reason = %s, reviewed_at = now() WHERE annotation_id = ANY(%s)",
                    (reason, annotation_ids),
                )
                if snapshot_ids:
                    cursor.execute(
                        "UPDATE training_snapshots SET state = 'revoked', revoke_reason = %s "
                        "WHERE snapshot_id = ANY(%s) AND state <> 'revoked'",
                        (reason, snapshot_ids),
                    )
                    cursor.execute(
                        "UPDATE adapter_manifests SET state = 'revoked', revoked_at = now(), "
                        "revoke_reason = %s WHERE snapshot_id = ANY(%s) AND state <> 'revoked' "
                        "RETURNING adapter_id",
                        (reason, snapshot_ids),
                    )
                    adapter_ids = [str(row["adapter_id"]) for row in cursor.fetchall()]
                    cursor.execute(
                        "UPDATE release_records SET status = 'rolled_back', updated_at = now(), "
                        "version = version + 1 WHERE training_snapshot_id = ANY(%s) "
                        "AND status IN ('candidate', 'shadow', 'canary', 'promoted') "
                        "RETURNING release_id",
                        (snapshot_ids,),
                    )
                    release_ids = [str(row["release_id"]) for row in cursor.fetchall()]
                else:
                    adapter_ids = []
                    release_ids = []
        result = {
            "annotations": sorted(annotation_ids),
            "snapshots": sorted(snapshot_ids),
            "adapters": sorted(adapter_ids),
            "releases": sorted(release_ids),
        }
        self.audit.record(identity, "training.source_revoked", "training_source", metadata=result)
        return result
