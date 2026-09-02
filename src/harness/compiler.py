"""Deterministic Experience-to-SFT compilation contracts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from core.evidence import EvidenceObjectStore, canonical_bytes, sha256
from harness.evaluation import model_fingerprint_digest, validate_model_fingerprint
from harness.experience import (
    _put_immutable,
    publish_experience_bundle,
    validate_experience_bundle,
    validate_task_bundle,
)

_HEX = frozenset("0123456789abcdef")


def _digest(value: Any) -> str:
    return sha256(canonical_bytes(value))


def _sha(value: Any, error: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ValueError(error)
    return value


def validate_gap_report(report: dict[str, Any]) -> dict[str, Any]:
    """Validate the compiler-facing subset of ``gap_report.v1``."""
    if not isinstance(report, dict) or report.get("schema_version") != "gap_report.v1":
        raise ValueError("compiler_gap_report_invalid")
    targets = report.get("targets")
    tasks = report.get("tasks")
    if not isinstance(targets, list) or len(targets) != 2 or not isinstance(tasks, list):
        raise ValueError("compiler_gap_report_invalid")
    target_digests = set()
    for target in targets:
        fingerprint = validate_model_fingerprint(target.get("fingerprint"))
        digest = model_fingerprint_digest(fingerprint)
        if target.get("fingerprint_sha256") != digest or digest in target_digests:
            raise ValueError("compiler_gap_target_invalid")
        target_digests.add(digest)
    seen = set()
    for task in tasks:
        task_id = task.get("task_bundle_id") if isinstance(task, dict) else None
        if (
            not isinstance(task_id, str)
            or not task_id.startswith("sha256:")
            or task_id in seen
            or task.get("split")
            not in {
                None,
                "train",
                "validation",
                "evaluation",
                "evaluation_holdout",
            }
            or task.get("classification") not in {"solved", "weak", "failed", "invalid"}
            or {item.get("target_fingerprint_sha256") for item in task.get("outcomes", [])}
            != target_digests
        ):
            raise ValueError("compiler_gap_task_invalid")
        seen.add(task_id)
    return deepcopy(report)


def authorize_experience_bundle(
    store: EvidenceObjectStore,
    bundle: dict[str, Any],
    *,
    source_ref: str,
    source_sha256: str,
    annotation: dict[str, Any],
) -> dict[str, str]:
    """Publish the immutable training-authorized view approved by one annotation."""
    bundle = validate_experience_bundle(bundle)
    if not isinstance(annotation, dict):
        raise ValueError("experience_training_authorization_invalid")
    label = annotation.get("label")
    if (
        annotation.get("tenant_id") != bundle["tenant_id"]
        or annotation.get("status") != "approved"
        or annotation.get("training_allowed") is not True
        or not isinstance(label, dict)
        or label.get("experience_ref") != source_ref
        or label.get("experience_sha256") != source_sha256
        or label.get("task_bundle_id") != bundle["task_bundle_id"]
        or label.get("run_id") != bundle["run_id"]
        or label.get("trial_id") != bundle["trial_id"]
        or label.get("decision") != "approved"
        or label.get("split") not in {"train", "validation"}
        or not isinstance(label.get("split_group"), str)
        or not label["split_group"]
        or not isinstance(label.get("expected_response"), str)
        or not label["expected_response"].strip()
    ):
        raise ValueError("experience_training_authorization_invalid")
    annotation_id = str(annotation.get("annotation_id", ""))
    if not annotation_id:
        raise ValueError("experience_training_annotation_missing")
    authorized = deepcopy(bundle)
    authorized["labels"]["training_allowed"] = True
    authorized["labels"]["annotation_refs"] = [annotation_id]
    return publish_experience_bundle(store, authorized)


def _model_messages(source: dict[str, Any]) -> list[dict[str, str]]:
    bundle = source["bundle"]
    if any(
        event["type"] == "model_call" and event["retry_of"] is not None
        for event in bundle["events"]
    ):
        raise ValueError("compiler_recovery_path_excluded")
    calls = []
    for event in bundle["events"]:
        if event["type"] != "model_call" or event["retry_of"] is not None:
            continue
        content = source["event_contents"].get(event["content_ref"])
        messages = content.get("request", {}).get("messages") if isinstance(content, dict) else None
        if (
            content is not None
            and content.get("status") == "succeeded"
            and isinstance(messages, list)
            and messages
            and all(
                isinstance(item, dict)
                and item.get("role") in {"system", "user", "assistant"}
                and isinstance(item.get("content"), str)
                for item in messages
            )
        ):
            calls.append(messages)
    if len(calls) != 1:
        raise ValueError("compiler_success_path_ambiguous")
    return calls[0]


def scope_rank_evidence(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Move evidence matching the task's declared document scope to the front."""
    ranked = deepcopy(messages)
    for message in ranked:
        content = message.get("content", "")
        if message.get("role") != "user" or "Document scope: " not in content:
            continue
        prefix, separator, remainder = content.partition("Evidence:\n")
        evidence, question_separator, question = remainder.partition("\nQuestion: ")
        if not separator or not question_separator:
            continue
        scope = question.splitlines()[0].removeprefix("Document scope: ").strip()
        lines = evidence.splitlines()
        lines.sort(key=lambda line: not line.partition("] ")[2].startswith(scope))
        message["content"] = f"{prefix}{separator}{'\n'.join(lines)}{question_separator}{question}"
    return ranked


def validate_compile_manifest(manifest: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
    """Validate an immutable ``compile_manifest.v1`` for SFT only."""
    required = {
        "schema_version",
        "algorithm",
        "compiler",
        "tenant_id",
        "target",
        "gap_report",
        "dataset",
        "sources",
        "exclusions",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ValueError("compile_manifest_fields_invalid")
    if manifest["schema_version"] != "compile_manifest.v1" or manifest["algorithm"] != "sft":
        raise ValueError("compile_manifest_schema_invalid")
    compiler = manifest["compiler"]
    compiler_fields = {
        "name",
        "version",
        "target_fingerprint_sha256",
        "selection",
        "recovery_policy",
        "prompt_transform",
        "config_sha256",
    }
    allowed_fields = {
        frozenset(compiler_fields),
        frozenset(compiler_fields - {"prompt_transform"}),
    }
    if (
        set(compiler) not in allowed_fields
        or compiler.get("name") != "sft-success"
        or compiler.get("version") != 1
    ):
        raise ValueError("compile_manifest_compiler_invalid")
    if compiler.get("prompt_transform", "identity") not in {
        "identity",
        "scope-ranked-evidence-v3",
    }:
        raise ValueError("compile_manifest_compiler_invalid")
    config_sha256 = _sha(compiler.get("config_sha256"), "compile_manifest_config_hash_invalid")
    if config_sha256 != _digest(
        {key: value for key, value in compiler.items() if key != "config_sha256"}
    ):
        raise ValueError("compile_manifest_config_hash_invalid")
    target = validate_model_fingerprint(manifest["target"].get("fingerprint"))
    if manifest["target"].get("fingerprint_sha256") != model_fingerprint_digest(target):
        raise ValueError("compile_manifest_target_invalid")
    _sha(manifest["gap_report"].get("sha256"), "compile_manifest_gap_hash_invalid")
    _sha(manifest["dataset"].get("sha256"), "compile_manifest_dataset_hash_invalid")
    if (
        manifest["dataset"].get("format") != "text-jsonl.v1"
        or not isinstance(manifest["dataset"].get("ref"), str)
        or not manifest["dataset"]["ref"]
        or manifest["dataset"].get("items", 0) < 2
        or set(manifest["dataset"].get("splits", {})) != {"train", "validation"}
        or min(manifest["dataset"]["splits"].values()) < 1
    ):
        raise ValueError("compile_manifest_dataset_invalid")
    sources = manifest["sources"]
    if not isinstance(sources, list) or len(sources) != manifest["dataset"]["items"]:
        raise ValueError("compile_manifest_sources_invalid")
    if len({item.get("task_bundle_id") for item in sources}) != len(sources):
        raise ValueError("compile_manifest_task_leakage")
    group_splits: dict[str, str] = {}
    for source in sources:
        if set(source) != {
            "experience_ref",
            "experience_sha256",
            "annotation_id",
            "task_bundle_id",
            "split",
            "split_group",
            "transform_sha256",
        }:
            raise ValueError("compile_manifest_sources_invalid")
        _sha(source.get("experience_sha256"), "compile_manifest_source_hash_invalid")
        _sha(source.get("transform_sha256"), "compile_manifest_transform_hash_invalid")
        if source.get("split") not in {"train", "validation"}:
            raise ValueError("compile_manifest_source_split_invalid")
        split_group = source.get("split_group")
        if not isinstance(split_group, str) or not split_group:
            raise ValueError("compile_manifest_split_group_invalid")
        if split_group in group_splits and group_splits[split_group] != source["split"]:
            raise ValueError("compile_manifest_split_contamination")
        group_splits[split_group] = source["split"]
    if not isinstance(manifest["exclusions"], dict) or any(
        not isinstance(key, str) or type(value) is not int or value < 0
        for key, value in manifest["exclusions"].items()
    ):
        raise ValueError("compile_manifest_exclusions_invalid")
    return deepcopy(manifest)


def validate_compile_decision(decision: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "decision",
        "reason",
        "config_sha256",
        "eligible",
        "exclusions",
        "target",
        "gap_report",
        "sources",
        "base_evaluation_id",
    }
    if not isinstance(decision, dict) or set(decision) != required:
        raise ValueError("compile_decision_fields_invalid")
    if decision["schema_version"] != "compile_decision.v1" or decision["decision"] != "NO-TRAIN":
        raise ValueError("compile_decision_invalid")
    if (
        not isinstance(decision["reason"], str)
        or type(decision["eligible"]) is not int
        or decision["eligible"] < 0
        or not isinstance(decision["exclusions"], dict)
    ):
        raise ValueError("compile_decision_invalid")
    _sha(decision["config_sha256"], "compile_decision_config_hash_invalid")
    _sha(decision["gap_report"].get("sha256"), "compile_decision_gap_hash_invalid")
    validate_model_fingerprint(decision["target"].get("fingerprint"))
    if decision["target"].get("fingerprint_sha256") != model_fingerprint_digest(
        decision["target"]["fingerprint"]
    ):
        raise ValueError("compile_decision_target_invalid")
    if not isinstance(decision["sources"], list) or any(
        not isinstance(item, dict)
        or set(item) != {"experience_ref", "experience_sha256", "annotation_id"}
        or not isinstance(item["experience_ref"], str)
        or not isinstance(item["annotation_id"], str)
        for item in decision["sources"]
    ):
        raise ValueError("compile_decision_sources_invalid")
    for item in decision["sources"]:
        _sha(item["experience_sha256"], "compile_decision_source_hash_invalid")
    if decision["reason"] == "target_release_policy_passed":
        if not isinstance(decision["base_evaluation_id"], str):
            raise ValueError("compile_decision_evaluation_missing")
    elif decision["base_evaluation_id"] is not None:
        raise ValueError("compile_decision_evaluation_unexpected")
    return deepcopy(decision)


def compile_sft_success(  # noqa: C901
    sources: list[dict[str, Any]],
    gap_report: dict[str, Any],
    *,
    gap_report_ref: str,
    gap_report_sha256: str,
    target_fingerprint_sha256: str,
    format_messages: Callable[[list[dict[str, str]]], str],
    target_policy_passed: bool = False,
    base_evaluation_id: str | None = None,
    include_reviewed_successes: bool = False,
    prompt_transform: str = "identity",
) -> dict[str, Any]:
    """Compile approved repair examples plus optional retention successes."""
    report = validate_gap_report(gap_report)
    _sha(gap_report_sha256, "compiler_gap_report_hash_invalid")
    target_entry = next(
        (
            item
            for item in report["targets"]
            if item["fingerprint_sha256"] == target_fingerprint_sha256
        ),
        None,
    )
    if target_entry is None:
        raise ValueError("compiler_target_not_in_gap_report")
    if len({item.get("tenant_id") for item in sources}) > 1:
        raise ValueError("compiler_tenant_mismatch")
    if prompt_transform not in {"identity", "scope-ranked-evidence-v3"}:
        raise ValueError("compiler_prompt_transform_invalid")
    config = {
        "name": "sft-success",
        "version": 1,
        "target_fingerprint_sha256": target_fingerprint_sha256,
        "selection": (
            "target-failed-plus-reviewed-success"
            if include_reviewed_successes
            else "target-failed-only"
        ),
        "recovery_policy": "exclude",
        "prompt_transform": prompt_transform,
    }
    source_descriptors = [
        {
            "experience_ref": item["experience_ref"],
            "experience_sha256": item["experience_sha256"],
            "annotation_id": str(item.get("annotation", {}).get("annotation_id", "")),
        }
        for item in sources
    ]
    decision_context = {
        "target": deepcopy(target_entry),
        "gap_report": {"ref": gap_report_ref, "sha256": gap_report_sha256},
        "sources": source_descriptors,
    }
    if target_policy_passed:
        if not base_evaluation_id:
            raise ValueError("compiler_base_evaluation_missing")
        return {
            "decision": "NO-TRAIN",
            "reason": "target_release_policy_passed",
            "config_sha256": _digest(config),
            "eligible": 0,
            "exclusions": {"target_release_policy_passed": len(sources)},
            "base_evaluation_id": base_evaluation_id,
            **decision_context,
        }

    gaps = {item["task_bundle_id"]: item for item in report["tasks"]}
    exclusions: dict[str, int] = {}
    compiled = []
    for source in sources:
        reason = None
        try:
            bundle = validate_experience_bundle(source.get("bundle"))
            task_bundle = validate_task_bundle(source.get("task_bundle"))
        except (TypeError, ValueError):
            reason = "source_invalid"
        if reason is None and bundle["tenant_id"] != source.get("tenant_id"):
            reason = "tenant_mismatch"
        gap = gaps.get(bundle["task_bundle_id"]) if reason is None else None
        target_outcome = next(
            (
                item
                for item in (gap or {}).get("outcomes", [])
                if item.get("target_fingerprint_sha256") == target_fingerprint_sha256
            ),
            None,
        )
        if reason is None and (
            gap is None
            or gap["classification"] == "invalid"
            or (gap["classification"] == "solved" and not include_reviewed_successes)
        ):
            reason = "gap_not_trainable"
        elif reason is None and (
            target_outcome is None
            or target_outcome.get("state")
            not in ({"failed", "succeeded"} if include_reviewed_successes else {"failed"})
        ):
            reason = "target_already_solved"
        elif reason is None and task_bundle["task"]["split"] == "evaluation_holdout":
            reason = "evaluation_holdout"
        elif reason is None and bundle["labels"]["training_allowed"] is not True:
            reason = "training_not_allowed"

        annotation = source.get("annotation", {})
        label = annotation.get("label", {}) if isinstance(annotation, dict) else {}
        annotation_id = str(annotation.get("annotation_id", ""))
        if reason is None and (
            annotation.get("status") != "approved"
            or annotation.get("training_allowed") is not True
            or annotation_id not in bundle["labels"]["annotation_refs"]
            or label.get("decision") != "approved"
            or label.get("task_bundle_id") != bundle["task_bundle_id"]
            or label.get("run_id") != bundle["run_id"]
            or label.get("trial_id") != bundle["trial_id"]
            or label.get("split") not in {"train", "validation"}
            or not isinstance(label.get("split_group"), str)
            or not label["split_group"]
            or not isinstance(label.get("expected_response"), str)
            or not label["expected_response"].strip()
            or not annotation.get("training_purpose")
            or not annotation.get("training_permission_version")
        ):
            reason = "annotation_unapproved"
        if reason is not None:
            exclusions[reason] = exclusions.get(reason, 0) + 1
            continue
        try:
            messages = _model_messages(source)
        except ValueError:
            exclusions["success_path_ambiguous"] = exclusions.get("success_path_ambiguous", 0) + 1
            continue
        if prompt_transform == "scope-ranked-evidence-v3":
            messages = scope_rank_evidence(messages)
        messages = [*messages, {"role": "assistant", "content": label["expected_response"]}]
        text = format_messages(messages)
        if not isinstance(text, str) or not text:
            raise ValueError("compiler_chat_template_failed")
        sample = {
            "text": text,
            "completion": label["expected_response"],
            "source": {
                "experience_ref": source["experience_ref"],
                "experience_sha256": source["experience_sha256"],
                "annotation_id": annotation_id,
                "task_bundle_id": bundle["task_bundle_id"],
                "split_group": label["split_group"],
            },
        }
        compiled.append({"split": label["split"], "sample": sample})

    if len({item["sample"]["source"]["task_bundle_id"] for item in compiled}) != len(compiled):
        exclusions["duplicate_task"] = len(compiled)
        compiled = []
    group_splits = {}
    for item in compiled:
        group = item["sample"]["source"]["split_group"]
        if group in group_splits and group_splits[group] != item["split"]:
            exclusions["split_contamination"] = len(compiled)
            compiled = []
            break
        group_splits[group] = item["split"]
    splits = {
        name: sum(item["split"] == name for item in compiled) for name in ("train", "validation")
    }
    if not compiled or min(splits.values()) < 1:
        return {
            "decision": "NO-TRAIN",
            "reason": "insufficient_eligible_splits",
            "config_sha256": _digest(config),
            "eligible": len(compiled),
            "exclusions": exclusions,
            "base_evaluation_id": None,
            **decision_context,
        }

    compiled.sort(key=lambda item: (item["split"], item["sample"]["source"]["task_bundle_id"]))
    dataset = b"".join(
        canonical_bytes({"split": item["split"], **item["sample"]}) + b"\n" for item in compiled
    )
    dataset_sha256 = sha256(dataset)
    dataset_ref = f"compiled/sft/sha256/{dataset_sha256}.jsonl"
    manifest = validate_compile_manifest(
        {
            "schema_version": "compile_manifest.v1",
            "algorithm": "sft",
            "compiler": {**config, "config_sha256": _digest(config)},
            "tenant_id": sources[0]["tenant_id"],
            "target": deepcopy(target_entry),
            "gap_report": {"ref": gap_report_ref, "sha256": gap_report_sha256},
            "dataset": {
                "ref": dataset_ref,
                "sha256": dataset_sha256,
                "size": len(dataset),
                "format": "text-jsonl.v1",
                "items": len(compiled),
                "splits": splits,
            },
            "sources": [
                {
                    **item["sample"]["source"],
                    "split": item["split"],
                    "transform_sha256": _digest(item["sample"]),
                }
                for item in compiled
            ],
            "exclusions": exclusions,
        }
    )
    return {"decision": "COMPILE", "dataset": dataset, "manifest": manifest}


def publish_compilation(
    store: EvidenceObjectStore, result: dict[str, Any], *, tenant_id: str
) -> dict[str, str]:
    """Publish either a content-addressed manifest+dataset or a NO-TRAIN decision."""
    if result.get("decision") == "NO-TRAIN":
        decision = validate_compile_decision({"schema_version": "compile_decision.v1", **result})
        body = canonical_bytes(decision)
        digest = sha256(body)
        ref = f"tenants/{tenant_id}/compiler/decisions/sha256/{digest}.json"
        _put_immutable(store, ref, body)
        return {"decision": "NO-TRAIN", "decision_ref": ref, "decision_sha256": digest}
    manifest = validate_compile_manifest(result.get("manifest"))
    dataset = result.get("dataset")
    if not isinstance(dataset, bytes) or sha256(dataset) != manifest["dataset"]["sha256"]:
        raise ValueError("compiler_dataset_hash_mismatch")
    dataset_ref = f"tenants/{tenant_id}/{manifest['dataset']['ref']}"
    manifest["dataset"]["ref"] = dataset_ref
    dataset_body = dataset
    _put_immutable(store, dataset_ref, dataset_body)
    manifest_body = canonical_bytes(manifest)
    manifest_sha256 = sha256(manifest_body)
    manifest_ref = f"tenants/{tenant_id}/compiler/manifests/sha256/{manifest_sha256}.json"
    _put_immutable(store, manifest_ref, manifest_body)
    return {
        "decision": "COMPILE",
        "dataset_ref": dataset_ref,
        "dataset_sha256": manifest["dataset"]["sha256"],
        "compile_manifest_ref": manifest_ref,
        "compile_manifest_sha256": manifest_sha256,
    }
