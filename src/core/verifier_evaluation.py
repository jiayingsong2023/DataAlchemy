"""Release and evaluation-evidence verifiers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .verifier_contracts import ReadOnlyServices, VerificationResult


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


def _gap_report(  # noqa: C901 - independent evidence checks stay linear
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


def _release_decision(  # noqa: C901 - independent evidence checks stay linear
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


def _experience_bundle(  # noqa: C901 - independent evidence checks stay linear
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


def _compile_manifest(  # noqa: C901 - independent evidence checks stay linear
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


def _compile_decision(  # noqa: C901 - independent evidence checks stay linear
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
