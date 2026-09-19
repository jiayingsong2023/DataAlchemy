from collections import Counter
from copy import deepcopy

import pytest

from core.evidence import canonical_bytes, sha256
from harness.engineering_suite import build_suite, candidate_input, verify_suite


def test_frozen_counts_labels_groups_and_reproducibility():
    suite = build_suite()
    assert suite == build_suite()
    # Updating this pin requires a new suite version, not accepting changed results.
    assert (
        suite["suite_sha256"] == "0137c459a6cac744ec03c933b8a16f247c8dbcf9a7f7129c21898ce923586a5c"
    )
    verify_suite(suite, suite["suite_sha256"])
    splits = suite["splits"]
    assert {key: len(value["cases"]) for key, value in splits.items()} == {
        "development": 20,
        "judge_calibration": 200,
        "candidate_holdout": 100,
    }
    groups = [{case["group_id"] for case in split["cases"]} for split in splits.values()]
    assert sum(map(len, groups)) == len(set.union(*groups))
    calibration = splits["judge_calibration"]["cases"]
    assert Counter(case["expected"]["judge_status"] for case in calibration) == {
        "pass": 100,
        "fail": 100,
    }
    assert Counter(
        case["category"] for case in calibration if case["expected"]["judge_status"] == "fail"
    ) == dict.fromkeys(
        (
            "entity",
            "number",
            "unit",
            "negation",
            "version",
            "citation",
            "no_evidence",
            "injection",
            "conflict",
            "distractor",
        ),
        10,
    )
    holdout = splits["candidate_holdout"]["cases"]
    assert Counter(case["language"] for case in holdout) == {"en": 50, "zh": 50}
    assert {case["expected"]["answerable"] for case in holdout} == {True, False}
    assert suite["descriptor"]["source"]["holdout_blind"] is False
    budget = suite["descriptor"]["policy"]["budget"]
    assert budget["cloud_egress_allowed"] is False
    assert budget["cost_limit_usd"] == budget["retries_max"] == 0
    for split in splits.values():
        for case in split["cases"]:
            assert not case["human_reviewed"] and not case["training_allowed"]
            assert set(candidate_input(case)) == {"case_id", "query", "evidence"}
            assert all(item["source_group"] == case["group_id"] for item in case["evidence"])


def test_program_generated_labels_correspond_to_actual_mutations():
    cases = build_suite()["splits"]["judge_calibration"]["cases"]
    for good, bad in zip(cases[::2], cases[1::2], strict=True):
        assert good["group_id"] == bad["group_id"]
        assert good["candidate_response"] != bad["candidate_response"]
        response = good["candidate_response"]
        if response["answer_status"] == "abstained":
            assert response["citations"] == []
        else:
            citation = response["citations"][0]
            matching = next(
                item for item in good["evidence"] if item["chunk_id"] == citation["chunk_id"]
            )
            assert citation["quote"] in matching["text"]
            assert response["answer"] == citation["quote"]


def test_pinned_digest_rejects_content_policy_and_hash_tampering():
    original = build_suite()
    for target in ("query", "policy", "hash"):
        tampered = deepcopy(original)
        if target == "query":
            tampered["splits"]["development"]["cases"][0]["query"] = "changed"
        elif target == "policy":
            tampered["descriptor"]["policy"]["candidate_pass_min"] = 0
        else:
            tampered["suite_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="engineering_suite_hash_mismatch"):
            verify_suite(tampered, original["suite_sha256"])
    tampered["suite_sha256"] = sha256(
        canonical_bytes({k: v for k, v in tampered.items() if k != "suite_sha256"})
    )
    tampered["descriptor"]["policy"]["candidate_pass_min"] = 0
    tampered["suite_sha256"] = sha256(
        canonical_bytes({k: v for k, v in tampered.items() if k != "suite_sha256"})
    )
    with pytest.raises(ValueError, match="engineering_suite_generator_mismatch"):
        verify_suite(tampered, tampered["suite_sha256"])
