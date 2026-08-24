from copy import deepcopy

import pytest

from core.evidence import ObjectNotFound, canonical_bytes, sha256
from core.verifiers import default_verifiers
from harness.compiler import compile_sft_success
from harness.evaluation import model_fingerprint_digest
from harness.model_migration import (
    base_arm_from_gap,
    build_migration_report,
    publish_migration_report,
)


class Store:
    def __init__(self):
        self.objects = {}

    def put(self, key, body):
        self.objects[key] = body

    def get(self, key):
        if key not in self.objects:
            raise ObjectNotFound(key)
        return self.objects[key]


class Services:
    def __init__(self, objects, trials=None):
        self.objects = objects
        self.trials = trials or {}

    def object_body(self, key):
        return self.objects.get(key)

    def trial(self, trial_id):
        return self.trials.get(trial_id)


def fingerprint(name="target", adapter=None):
    return {
        "schema_version": "model_fingerprint.v1",
        "model_id": name,
        "model_sha256": "1" * 64,
        "tokenizer_sha256": "2" * 64,
        "chat_template_sha256": "3" * 64,
        "adapter_sha256": adapter,
    }


def evidence():
    target, other = fingerprint(), fingerprint("other")
    target_sha = model_fingerprint_digest(target)
    other_sha = model_fingerprint_digest(other)
    verifier_sha = "4" * 64
    generation_sha = "5" * 64
    transcripts = {}
    outcomes = []
    trials = {}
    for name, model, model_sha in (
        ("target", target, target_sha),
        ("other", other, other_sha),
    ):
        transcript = {
            "model_fingerprint": model,
            "generation_policy_sha256": generation_sha,
            "verifier": {"contract_digest": verifier_sha},
            "latency_ms": 100.0,
        }
        transcript_ref = f"transcripts/{name}.json"
        transcript_sha = sha256(canonical_bytes(transcript))
        trial_id = f"trial-{name}"
        transcripts[transcript_ref] = transcript
        outcomes.append(
            {
                "target_fingerprint_sha256": model_sha,
                "state": "failed",
                "environment_initial_state_sha256": "6" * 64,
                "transcript_ref": transcript_ref,
                "transcript_sha256": transcript_sha,
                "trial_id": trial_id,
            }
        )
        trials[trial_id] = {
            "tenant_id": "acme",
            "state": "failed",
            "fingerprint": {
                "task_bundle_id": "sha256:" + "a" * 64,
                "model_fingerprint_sha256": model_sha,
            },
            "transcript_key": transcript_ref,
            "transcript_sha256": transcript_sha,
        }
    gap = {
        "schema_version": "gap_report.v1",
        "targets": [
            {"fingerprint_sha256": target_sha, "fingerprint": target},
            {"fingerprint_sha256": other_sha, "fingerprint": other},
        ],
        "generation_policy_sha256": generation_sha,
        "verifier": {
            "name": "verify_rag_outcome",
            "version": 1,
            "contract_digest": verifier_sha,
        },
        "tasks": [
            {
                "task_bundle_id": "sha256:" + "a" * 64,
                "case_id": "case-a",
                "classification": "failed",
                "outcomes": outcomes,
            }
        ],
        "metrics": {"valid_tasks": 1, "invalid_tasks": 0, "capability_denominator": 1},
    }
    gap_ref = "gap.json"
    gap_sha = sha256(canonical_bytes(gap))
    compiled = compile_sft_success(
        [],
        gap,
        gap_report_ref=gap_ref,
        gap_report_sha256=gap_sha,
        target_fingerprint_sha256=target_sha,
        format_messages=lambda messages: str(messages),
    )
    decision = {"schema_version": "compile_decision.v1", **compiled}
    decision_ref = "decision.json"
    decision_sha = sha256(canonical_bytes(decision))
    base = base_arm_from_gap(
        gap,
        target_sha,
        transcripts,
        gap_report_ref=gap_ref,
        gap_report_sha256=gap_sha,
    )
    source = {
        "kind": "compile_decision",
        "ref": decision_ref,
        "sha256": decision_sha,
        "reason": decision["reason"],
        "value": decision,
    }
    return target, gap, transcripts, trials, base, source


def policy():
    return {
        "version": "model-migration@1",
        "min_pass_rate": 1.0,
        "min_improvement": 0.1,
        "max_p95_regression_ratio": 1.2,
        "max_training_cost": 10.0,
    }


def manifest_source(target):
    target_sha = model_fingerprint_digest(target)
    compiler = {
        "name": "sft-success",
        "version": 1,
        "target_fingerprint_sha256": target_sha,
        "selection": "target-failed-only",
        "recovery_policy": "exclude",
    }
    compiler["config_sha256"] = sha256(canonical_bytes(compiler))
    manifest = {
        "schema_version": "compile_manifest.v1",
        "algorithm": "sft",
        "compiler": compiler,
        "tenant_id": "acme",
        "target": {"fingerprint": target, "fingerprint_sha256": target_sha},
        "gap_report": {"ref": "gap.json", "sha256": "7" * 64},
        "dataset": {
            "ref": "dataset.jsonl",
            "sha256": "8" * 64,
            "size": 2,
            "format": "text-jsonl.v1",
            "items": 2,
            "splits": {"train": 1, "validation": 1},
        },
        "sources": [
            {
                "experience_ref": f"experience-{split}.json",
                "experience_sha256": "9" * 64,
                "annotation_id": f"annotation-{split}",
                "task_bundle_id": "sha256:" + marker * 64,
                "split": split,
                "transform_sha256": "b" * 64,
            }
            for split, marker in (("train", "a"), ("validation", "b"))
        ],
        "exclusions": {},
    }
    return {
        "kind": "compile_manifest",
        "ref": "manifest.json",
        "sha256": sha256(canonical_bytes(manifest)),
        "value": manifest,
    }


def test_real_no_candidate_path_is_deterministic_blocked_and_independently_verified():
    target, gap, transcripts, trials, base, source = evidence()
    report = build_migration_report(
        tenant_id="acme",
        target_fingerprint=target,
        learning_source=source,
        arms=[base],
        policy=policy(),
    )
    assert report["decision"] == {
        "status": "BLOCKED",
        "reason": "candidate_unavailable",
        "selected_arm": None,
    }

    store = Store()
    published = publish_migration_report(store, report)
    assert publish_migration_report(store, deepcopy(report)) == published
    objects = {
        **store.objects,
        "gap.json": canonical_bytes(gap),
        "decision.json": canonical_bytes(source["value"]),
        **{ref: canonical_bytes(value) for ref, value in transcripts.items()},
    }
    checked = (
        default_verifiers()
        .get("verify_model_migration", 1)
        .handler(
            {"parameters": published},
            {"tenant_id": "acme"},
            {},
            Services(objects, trials),
        )
    )
    assert checked.status == "passed"
    assert checked.summary["status"] == "BLOCKED"


def test_candidate_policy_produces_go_no_go_and_no_train():
    target, _gap, _transcripts, _trials, base, _source = evidence()
    source = manifest_source(target)
    candidate = deepcopy(base)
    candidate.update(
        {
            "name": "gap_sft",
            "subject_type": "adapter",
            "subject_ref": "adapter-1",
            "fingerprint_sha256": model_fingerprint_digest(fingerprint(adapter="c" * 64)),
            "evidence": {"kind": "evaluation", "ref": "eval-1", "sha256": "d" * 64},
            "metrics": {
                "pass_rate": 1.0,
                "p95_latency_ms": 110.0,
                "training_cost": 5.0,
            },
            "hard_gates": {"passed": True},
        }
    )
    report = build_migration_report(
        tenant_id="acme",
        target_fingerprint=target,
        learning_source=source,
        arms=[base, candidate],
        policy=policy(),
    )
    assert report["decision"]["status"] == "GO"

    candidate["metrics"]["pass_rate"] = 0.0
    no_go = build_migration_report(
        tenant_id="acme",
        target_fingerprint=target,
        learning_source=source,
        arms=[base, candidate],
        policy=policy(),
    )
    assert no_go["decision"]["status"] == "NO-GO"

    passed_base = deepcopy(base)
    passed_base["metrics"]["pass_rate"] = 1.0
    passed_base["hard_gates"]["passed"] = True
    no_train = build_migration_report(
        tenant_id="acme",
        target_fingerprint=target,
        learning_source=source,
        arms=[passed_base, candidate],
        policy=policy(),
    )
    assert no_train["decision"]["status"] == "NO-TRAIN"


def test_alignment_and_transcript_tampering_fail_closed():
    target, gap, transcripts, _trials, base, source = evidence()
    broken = deepcopy(base)
    broken["valid_trials"] = 0
    with pytest.raises(ValueError, match="migration_arm_trials_invalid"):
        build_migration_report(
            tenant_id="acme",
            target_fingerprint=target,
            learning_source=source,
            arms=[broken],
            policy=policy(),
        )
    transcripts["transcripts/target.json"]["latency_ms"] = 99
    with pytest.raises(ValueError, match="migration_base_transcript_mismatch"):
        base_arm_from_gap(
            gap,
            model_fingerprint_digest(target),
            transcripts,
            gap_report_ref="gap.json",
            gap_report_sha256=sha256(canonical_bytes(gap)),
        )
