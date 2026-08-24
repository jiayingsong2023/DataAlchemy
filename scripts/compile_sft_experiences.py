"""Compile approved gap-only Experience Bundles into one governed SFT snapshot."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import S3EvidenceStore, sha256
from core.verifiers import ReadOnlyServices, default_verifiers
from harness.compiler import (
    authorize_experience_bundle,
    compile_sft_success,
    publish_compilation,
)
from harness.evaluation import (
    EvaluationService,
    model_fingerprint_digest,
    model_path_fingerprint,
)
from harness.experience import validate_experience_bundle, validate_task_bundle
from utils.s3_utils import S3Utils


def _read(services: ReadOnlyServices, ref: str, expected_sha256: str | None = None) -> bytes:
    body = services.object_body(ref)
    if body is None or (expected_sha256 and sha256(body) != expected_sha256):
        raise ValueError(f"compiler_object_hash_mismatch:{ref}")
    return body


def _format_messages(tokenizer, messages: list[dict[str, str]]) -> str:
    if tokenizer.chat_template:
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False
        )
    eos = tokenizer.eos_token or ""
    return "\n".join(f"{item['role']}: {item['content']}" for item in messages) + eos


def main() -> None:  # noqa: C901 - linear fail-closed CLI gate
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gap-report-ref", required=True)
    parser.add_argument("--gap-report-sha256", required=True)
    parser.add_argument("--target-fingerprint-sha256", required=True)
    parser.add_argument("--experience-ref", action="append", default=[])
    parser.add_argument("--experience-sha256", action="append", default=[])
    parser.add_argument("--annotation-id", action="append", default=[])
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--username", default="el2-compiler")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument(
        "--verifier-database-url", default=os.getenv("VERIFIER_DATABASE_URL")
    )
    parser.add_argument("--model-root", required=True)
    parser.add_argument("--base-evaluation-id")
    args = parser.parse_args()
    if not args.database_url:
        raise ValueError("compiler_database_url_missing")
    if not args.verifier_database_url or args.verifier_database_url == args.database_url:
        raise ValueError("compiler_verifier_database_url_missing")
    if len(args.experience_ref) != len(args.experience_sha256):
        raise ValueError("compiler_experience_descriptor_mismatch")

    identity = {"tenant_id": args.tenant_id, "username": args.username, "role": "admin"}
    services = ReadOnlyServices(args.verifier_database_url, identity)
    s3 = S3Utils()
    store = S3EvidenceStore(s3.bucket, s3.client)
    gap_body = _read(services, args.gap_report_ref, args.gap_report_sha256)
    gap_report = json.loads(gap_body)
    target = next(
        (
            item
            for item in gap_report.get("targets", [])
            if item.get("fingerprint_sha256") == args.target_fingerprint_sha256
        ),
        None,
    )
    if target is None:
        raise ValueError("compiler_target_not_in_gap_report")
    actual_fingerprint = model_path_fingerprint(
        target["fingerprint"]["model_id"], model_root=args.model_root
    )
    if model_fingerprint_digest(actual_fingerprint) != args.target_fingerprint_sha256:
        raise ValueError("compiler_target_fingerprint_mismatch")
    if args.base_evaluation_id:
        evaluation = services.evaluation(args.base_evaluation_id)
        if (
            evaluation is None
            or evaluation["subject_type"] != "base"
            or evaluation["subject_ref"] != args.target_fingerprint_sha256
            or evaluation["state"] != "passed"
            or evaluation.get("hard_gates", {}).get("passed") is not True
        ):
            raise ValueError("compiler_base_evaluation_unverified")

    annotations = {}
    for annotation_id in args.annotation_id:
        annotation = services.annotation(annotation_id)
        if annotation is None:
            raise ValueError(f"compiler_annotation_missing:{annotation_id}")
        content = _read(services, annotation["content_key"], annotation["content_sha256"])
        if json.loads(content) != annotation["label"]:
            raise ValueError(f"compiler_annotation_content_mismatch:{annotation_id}")
        annotations[annotation_id] = annotation

    sources = []
    verifier = default_verifiers().get("verify_experience_bundle", 1)
    for experience_ref, experience_sha256 in zip(
        args.experience_ref, args.experience_sha256, strict=True
    ):
        bundle = validate_experience_bundle(
            json.loads(_read(services, experience_ref, experience_sha256))
        )
        annotation = next(
            (
                value
                for value in annotations.values()
                if value["label"].get("experience_ref") == experience_ref
                and value["label"].get("experience_sha256") == experience_sha256
            ),
            {},
        )
        if annotation and bundle["labels"]["training_allowed"] is not True:
            descriptor = authorize_experience_bundle(
                store,
                bundle,
                source_ref=experience_ref,
                source_sha256=experience_sha256,
                annotation=annotation,
            )
            experience_ref = descriptor["experience_ref"]
            experience_sha256 = descriptor["experience_sha256"]
            bundle = validate_experience_bundle(json.loads(_read(services, experience_ref)))
        verified = verifier.handler(
            {
                "parameters": {
                    "experience_ref": experience_ref,
                    "experience_sha256": experience_sha256,
                }
            },
            {"tenant_id": args.tenant_id},
            {},
            services,
        )
        if verified.status != "passed":
            raise ValueError(f"compiler_experience_unverified:{verified.error_code}")
        task_bundle = validate_task_bundle(
            json.loads(_read(services, bundle["task_bundle_ref"], bundle["task_bundle_sha256"]))
        )
        event_contents = {
            event["content_ref"]: json.loads(
                _read(services, event["content_ref"], event["sha256"])
            )
            for event in bundle["events"]
        }
        sources.append(
            {
                "tenant_id": args.tenant_id,
                "experience_ref": experience_ref,
                "experience_sha256": experience_sha256,
                "bundle": bundle,
                "task_bundle": task_bundle,
                "annotation": annotation,
                "event_contents": event_contents,
            }
        )

    tokenizer = None

    def format_messages(messages):
        nonlocal tokenizer
        if tokenizer is None:
            from transformers import AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(
                actual_fingerprint["model_id"], local_files_only=True
            )
        return _format_messages(tokenizer, messages)

    result = compile_sft_success(
        sources,
        gap_report,
        gap_report_ref=args.gap_report_ref,
        gap_report_sha256=args.gap_report_sha256,
        target_fingerprint_sha256=args.target_fingerprint_sha256,
        format_messages=format_messages,
        target_policy_passed=bool(args.base_evaluation_id),
        base_evaluation_id=args.base_evaluation_id,
    )
    published = publish_compilation(store, result, tenant_id=args.tenant_id)
    if published["decision"] == "NO-TRAIN":
        checked = default_verifiers().get("verify_compile_decision", 1).handler(
            {
                "parameters": {
                    "decision_ref": published["decision_ref"],
                    "decision_sha256": published["decision_sha256"],
                }
            },
            {"tenant_id": args.tenant_id},
            {},
            services,
        )
        if checked.status != "passed":
            raise RuntimeError(f"compiler_decision_verification_failed:{checked.error_code}")
        print(json.dumps({**published, **result}, ensure_ascii=False, sort_keys=True))
        return

    manifest = result["manifest"]
    items = []
    for source in manifest["sources"]:
        annotation = annotations[source["annotation_id"]]
        items.append(
            {
                "item_id": source["experience_sha256"],
                "split": source["split"],
                "source_type": "trajectory_annotation",
                "source_id": source["annotation_id"],
                "source_sha256": annotation["content_sha256"],
                "source_acl_digest": annotation.get("source_acl_digest"),
                "training_allowed": True,
                "training_purpose": annotation["training_purpose"],
                "training_permission_version": annotation["training_permission_version"],
                "transform_digest": source["transform_sha256"],
            }
        )
    snapshot_id = EvaluationService(args.database_url).create_snapshot(
        identity,
        annotation_items=items,
        dataset_key=published["dataset_ref"],
        dataset_sha256=published["dataset_sha256"],
        dataset_size=manifest["dataset"]["size"],
        base_model_digest=actual_fingerprint["model_sha256"],
        policy_version="sft-success@1",
        compile_manifest_key=published["compile_manifest_ref"],
        compile_manifest_sha256=published["compile_manifest_sha256"],
        target_tokenizer_digest=actual_fingerprint["tokenizer_sha256"],
        chat_template_digest=actual_fingerprint["chat_template_sha256"],
    )
    checked = default_verifiers().get("verify_compile_manifest", 1).handler(
        {
            "parameters": {
                "snapshot_id": snapshot_id,
                "compile_manifest_ref": published["compile_manifest_ref"],
                "compile_manifest_sha256": published["compile_manifest_sha256"],
            }
        },
        {"tenant_id": args.tenant_id},
        {},
        services,
    )
    if checked.status != "passed":
        raise RuntimeError(f"compiler_snapshot_verification_failed:{checked.error_code}")
    print(json.dumps({**published, "snapshot_id": snapshot_id}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
