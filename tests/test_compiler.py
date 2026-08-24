import json
from copy import deepcopy

import pytest

from src.core.evidence import ObjectNotFound, canonical_bytes, sha256
from src.core.verifiers import VerificationResult, default_verifiers
from src.harness.compiler import (
    authorize_experience_bundle,
    compile_sft_success,
    publish_compilation,
    validate_compile_manifest,
)
from src.harness.evaluation import model_fingerprint_digest


class Store:
    def __init__(self):
        self.objects = {}

    def get(self, key):
        if key not in self.objects:
            raise ObjectNotFound(key)
        return self.objects[key]

    def put(self, key, body):
        self.objects[key] = body


def fingerprint(name="target"):
    marker = "a" if name == "target" else "b"
    return {
        "schema_version": "model_fingerprint.v1",
        "model_id": name,
        "model_sha256": marker * 64,
        "tokenizer_sha256": "c" * 64,
        "chat_template_sha256": "d" * 64,
        "adapter_sha256": None,
    }


def task_bundle(task_id, split="train"):
    return {
        "schema_version": "task_bundle.v1",
        "task": {
            "case_id": task_id,
            "type": "rag",
            "input_ref": f"{task_id}/input.json",
            "input_sha256": "1" * 64,
            "input_tenant_id": "acme",
            "split": split,
        },
        "environment": {
            "snapshot_ref": f"{task_id}/environment.json",
            "snapshot_sha256": "2" * 64,
            "snapshot_tenant_id": "acme",
            "reset_contract": {
                "kind": "registered-script",
                "ref": "reset@1",
                "sha256": "3" * 64,
            },
        },
        "tools": [{"name": "rag", "version": 1, "contract_sha256": "4" * 64}],
        "verifiers": [
            {"name": "verify_rag_outcome", "version": 1, "contract_sha256": "5" * 64}
        ],
        "limits": {"max_steps": 1, "deadline_seconds": 30},
        "governance": {
            "tenant_id": "acme",
            "acl_sha256": "6" * 64,
            "permission_version": "permission-1",
            "retention_until": "2030-01-01T00:00:00Z",
        },
    }


def experience(task_id, annotation_id, *, allowed=True):
    bundle_id = "sha256:" + task_id * 64
    model = fingerprint("source")
    call_ref = f"{task_id}/model.json"
    bundle = {
        "schema_version": "experience_bundle.v1",
        "tenant_id": "acme",
        "task_bundle_id": bundle_id,
        "task_bundle_ref": f"{task_id}/bundle.json",
        "task_bundle_sha256": task_id * 64,
        "run_id": f"run-{task_id}",
        "trial_id": f"trial-{task_id}",
        "source_manifest_ref": f"{task_id}/manifest.json",
        "source_manifest_sha256": "7" * 64,
        "producer": {
            **{key: model[key] for key in model if key != "schema_version"},
            "policy_sha256": "8" * 64,
        },
        "environment": {"receipt_ref": f"{task_id}/receipt.json", "receipt_sha256": "9" * 64},
        "events": [
            {
                "sequence": sequence,
                "type": event_type,
                "content_ref": call_ref if event_type == "model_call" else f"{task_id}/{event_type}.json",
                "sha256": str(sequence) * 64,
                "producer": "test",
                "call_id": "call-1" if event_type == "model_call" else None,
                "retry_of": None,
                "parent_call_id": "call-1" if event_type in {"tool_observation", "verifier_result", "rollout_finished"} else None,
            }
            for sequence, event_type in enumerate(
                ["context_built", "model_call", "tool_observation", "verifier_result", "rollout_finished"],
                1,
            )
        ],
        "outcome": {
            "state": "failed",
            "verifier_ref": f"{task_id}/verifier.json",
            "verifier_sha256": "e" * 64,
            "reward": {"task": 0},
        },
        "labels": {
            "success": False,
            "failure_code": "wrong_answer",
            "training_allowed": allowed,
            "annotation_refs": [annotation_id] if allowed else [],
        },
    }
    annotation = {
        "annotation_id": annotation_id,
        "tenant_id": "acme",
        "status": "approved",
        "training_allowed": True,
        "training_purpose": "model_improvement",
        "training_permission_version": "permission-1",
        "label": {
            "decision": "approved",
            "task_bundle_id": bundle_id,
            "run_id": f"run-{task_id}",
            "trial_id": f"trial-{task_id}",
            "split": "train" if task_id == "a" else "validation",
            "expected_response": f"correct-{task_id}",
        },
    }
    event = {
        "schema_version": "model_call.v1",
        "request": {"messages": [{"role": "user", "content": f"question-{task_id}"}]},
        "response": {"content": "wrong"},
        "status": "succeeded",
    }
    return bundle, annotation, {call_ref: event}


def report():
    target, source = fingerprint(), fingerprint("source")
    targets = [
        {"fingerprint_sha256": model_fingerprint_digest(item), "fingerprint": item}
        for item in (target, source)
    ]
    return {
        "schema_version": "gap_report.v1",
        "targets": targets,
        "generation_policy_sha256": "f" * 64,
        "verifier": {"name": "verify_rag_outcome", "version": 1, "contract_digest": "1" * 64},
        "tasks": [
            {
                "task_bundle_id": "sha256:" + task_id * 64,
                "case_id": f"case-{task_id}",
                "classification": "failed",
                "outcomes": [
                    {
                        "target_fingerprint_sha256": target["fingerprint_sha256"],
                        "state": "failed",
                    }
                    for target in targets
                ],
            }
            for task_id in ("a", "b")
        ],
        "metrics": {"valid_tasks": 2, "invalid_tasks": 0, "capability_denominator": 2},
    }


def sources(*, allowed=True):
    values = []
    for task_id in ("a", "b"):
        bundle, annotation, contents = experience(task_id, f"annotation-{task_id}", allowed=allowed)
        values.append(
            {
                "tenant_id": "acme",
                "experience_ref": f"{task_id}/experience.json",
                "experience_sha256": sha256(canonical_bytes(bundle)),
                "bundle": bundle,
                "task_bundle": task_bundle(task_id),
                "annotation": annotation,
                "event_contents": contents,
            }
        )
    return values


def compile_result(values=None):
    gap = report()
    return compile_sft_success(
        values or sources(),
        gap,
        gap_report_ref="gap.json",
        gap_report_sha256=sha256(canonical_bytes(gap)),
        target_fingerprint_sha256=model_fingerprint_digest(fingerprint()),
        format_messages=lambda messages: json.dumps(messages, sort_keys=True),
    )


def test_compiler_is_deterministic_and_keeps_lineage_out_of_text():
    first = compile_result()
    second = compile_result()
    assert first == second
    assert first["decision"] == "COMPILE"
    assert validate_compile_manifest(first["manifest"]) == first["manifest"]
    records = [json.loads(line) for line in first["dataset"].splitlines()]
    assert {item["source"]["task_bundle_id"] for item in records} == {
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    }
    assert {item["split"] for item in records} == {"train", "validation"}
    assert all("wrong" not in item["text"] for item in records)


def test_compiler_returns_no_train_for_unapproved_duplicate_or_solved_target():
    assert compile_result(sources(allowed=False))["decision"] == "NO-TRAIN"
    duplicated = sources()
    duplicated[1]["bundle"]["task_bundle_id"] = duplicated[0]["bundle"]["task_bundle_id"]
    assert compile_result(duplicated)["decision"] == "NO-TRAIN"
    gap = report()
    result = compile_sft_success(
        sources(),
        gap,
        gap_report_ref="gap.json",
        gap_report_sha256=sha256(canonical_bytes(gap)),
        target_fingerprint_sha256=model_fingerprint_digest(fingerprint()),
        format_messages=lambda messages: str(messages),
        target_policy_passed=True,
        base_evaluation_id="evaluation-1",
    )
    assert result["reason"] == "target_release_policy_passed"


@pytest.mark.parametrize("blocked", ["holdout", "solved", "revoked"])
def test_compiler_excludes_non_training_sources(blocked):
    values = sources()
    gap = report()
    if blocked == "holdout":
        values[0]["task_bundle"]["task"]["split"] = "evaluation_holdout"
    elif blocked == "solved":
        gap["tasks"][0]["classification"] = "solved"
        for outcome in gap["tasks"][0]["outcomes"]:
            outcome["state"] = "succeeded"
    else:
        values[0]["annotation"]["status"] = "revoked"
    result = compile_sft_success(
        values,
        gap,
        gap_report_ref="gap.json",
        gap_report_sha256=sha256(canonical_bytes(gap)),
        target_fingerprint_sha256=model_fingerprint_digest(fingerprint()),
        format_messages=lambda messages: str(messages),
    )
    assert result["decision"] == "NO-TRAIN"


def test_authorization_and_publication_are_content_addressed():
    store = Store()
    bundle, annotation, _ = experience("a", "annotation-a", allowed=False)
    source_body = canonical_bytes(bundle)
    source_ref, source_hash = "source.json", sha256(source_body)
    store.put(source_ref, source_body)
    annotation["label"].update(
        {"experience_ref": source_ref, "experience_sha256": source_hash}
    )
    descriptor = authorize_experience_bundle(
        store,
        bundle,
        source_ref=source_ref,
        source_sha256=source_hash,
        annotation=annotation,
    )
    authorized = json.loads(store.get(descriptor["experience_ref"]))
    assert authorized["labels"]["training_allowed"] is True
    published = publish_compilation(store, compile_result(), tenant_id="acme")
    assert sha256(store.get(published["dataset_ref"])) == published["dataset_sha256"]
    assert sha256(store.get(published["compile_manifest_ref"])) == published[
        "compile_manifest_sha256"
    ]


def test_manifest_rejects_cross_split_task_leakage():
    manifest = compile_result()["manifest"]
    broken = deepcopy(manifest)
    broken["sources"][1]["task_bundle_id"] = broken["sources"][0]["task_bundle_id"]
    with pytest.raises(ValueError, match="compile_manifest_task_leakage"):
        validate_compile_manifest(broken)
    broken = deepcopy(manifest)
    broken["compiler"]["selection"] = "all"
    with pytest.raises(ValueError, match="compile_manifest_config_hash_invalid"):
        validate_compile_manifest(broken)


def test_independent_compile_verifier_rechecks_live_authorization(monkeypatch):
    store = Store()
    gap = report()
    store.put("gap.json", canonical_bytes(gap))
    result = compile_result()
    descriptor = publish_compilation(store, result, tenant_id="acme")
    annotations = {}
    for source in sources():
        annotation = source["annotation"]
        content = canonical_bytes(annotation["label"])
        content_key = f"annotations/{annotation['annotation_id']}.json"
        store.put(content_key, content)
        annotations[annotation["annotation_id"]] = {
            **annotation,
            "content_key": content_key,
            "content_sha256": sha256(content),
        }

    class Services:
        def object_body(self, key):
            return store.objects.get(key)

        def annotation(self, annotation_id):
            return annotations.get(annotation_id)

        def snapshot(self, _snapshot_id):
            return None

    monkeypatch.setattr(
        "src.core.verifiers._experience_bundle",
        lambda criterion, *_: VerificationResult(
            "passed",
            {
                "training_allowed": True,
                "run_id": f"run-{criterion['parameters']['experience_ref'][0]}",
                "trial_id": f"trial-{criterion['parameters']['experience_ref'][0]}",
            },
        ),
    )
    spec = default_verifiers().get("verify_compile_manifest", 1)
    parameters = {
        "compile_manifest_ref": descriptor["compile_manifest_ref"],
        "compile_manifest_sha256": descriptor["compile_manifest_sha256"],
    }
    passed = spec.handler({"parameters": parameters}, {"tenant_id": "acme"}, {}, Services())
    assert passed.status == "passed"
    annotations["annotation-a"]["status"] = "revoked"
    revoked = spec.handler({"parameters": parameters}, {"tenant_id": "acme"}, {}, Services())
    assert revoked.error_code == "compile_annotation_unapproved"
