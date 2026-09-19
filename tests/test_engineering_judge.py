"""Scripted parser/arithmetic checks only; no judge accuracy or holdout experiment."""

import json
from copy import deepcopy

import pytest

from core.evidence import canonical_bytes, sha256
from core.verifiers import default_verifiers
from harness.engineering_judge import (
    calibration_report,
    replay_calibration,
    swapped_preference,
    validate_judgment,
)
from harness.engineering_suite import build_suite

MODEL = {
    "model_id": "scripted-unit-test",
    "provider_version": "not-a-real-provider",
    "family": "test",
    "candidate_family": "different-test",
}


def scripted(case):
    response = case["candidate_response"]
    positive = case["expected"]["judge_status"] == "pass"
    evidence = case["evidence"]
    return {
        "case_id": case["case_id"],
        "claims": []
        if response["answer_status"] == "abstained"
        else [
            {
                "text": response["answer"],
                "status": "supported" if evidence else "insufficient_evidence",
                "chunk_id": evidence[0]["chunk_id"] if evidence else None,
                "quote": evidence[0]["text"] if evidence else None,
            }
        ],
        "question_covered": positive,
        "abstention_appropriate": positive,
        "injection_detected": case["category"] == "injection",
    }


def test_strict_claim_schema_and_derived_status():
    case = build_suite()["splits"]["judge_calibration"]["cases"][0]
    value = scripted(case)
    assert validate_judgment(case, json.dumps(value))["status"] == "pass"
    value["claims"][0]["status"] = "contradicted"
    assert validate_judgment(case, json.dumps(value))["status"] == "fail"
    value["claims"][0].update(status="insufficient_evidence", chunk_id=None, quote=None)
    assert validate_judgment(case, json.dumps(value))["status"] == "uncertain"
    invalid = [None, "bad-json", "[]", "{}", "NaN", "[" * 10000]
    for patch in (
        {"claims": []},
        {"case_id": "other"},
        {"confidence": 1.0},
        {"question_covered": 1},
        {"question_covered": float("nan")},
    ):
        invalid.append(json.dumps({**scripted(case), **patch}))
    for patch in (
        {"chunk_id": "fabricated"},
        {"quote": "not in evidence"},
        {"quote": ""},
        {"text": "not in answer"},
        {"status": "pass"},
        {"execute": "ignore rules"},
        {"text": "Device"},
    ):
        changed = scripted(case)
        changed["claims"][0].update(patch)
        invalid.append(json.dumps(changed))
    raw = json.dumps(scripted(case))
    invalid.append(
        raw.replace(
            '"question_covered": true', '"question_covered": false, "question_covered": true'
        )
    )
    assert all(validate_judgment(case, raw)["status"] == "invalid" for raw in invalid)


def test_candidate_citation_hard_gate_cannot_be_overruled_by_judge():
    case = next(
        case
        for case in build_suite()["splits"]["judge_calibration"]["cases"]
        if case["category"] == "citation" and case["expected"]["judge_status"] == "fail"
    )
    value = scripted(case)
    value.update(question_covered=True, abstention_appropriate=True)
    assert validate_judgment(case, json.dumps(value))["status"] == "fail"


def test_position_swap_consistency_never_selects_favorable_order():
    assert swapped_preference("A", "B") == "candidate"
    assert swapped_preference("B", "A") == "base"
    assert swapped_preference("tie", "tie") == "tie"
    assert swapped_preference("A", "A") == "uncertain"
    assert swapped_preference("tie", "B") == "uncertain"
    assert swapped_preference("pass", "A") == "invalid"


def test_calibration_full_denominators_categories_missing_and_replay():
    suite = build_suite()
    cases = suite["splits"]["judge_calibration"]["cases"]
    outputs = [{"case_id": case["case_id"], "raw": json.dumps(scripted(case))} for case in cases]
    kwargs = {
        "expected_suite_sha256": suite["suite_sha256"],
        "model": MODEL,
        "prompt": "scripted unit test",
    }
    report = calibration_report(suite, outputs, **kwargs)
    assert report["calibration_decision"] == "PASS"
    assert report["metrics"]["total"] == 200
    assert report["metrics"]["source_groups"] == 100
    assert report["metrics"]["positive_acceptance"]["denominator"] == 100
    assert report["metrics"]["negative_acceptance"]["denominator"] == 100
    assert report["metrics"]["negative_acceptance"]["wilson_95"][1] > 0
    assert not report["human_reviewed"] and report["judge_only"]
    assert not report["independent_semantic_verification"]
    assert report["categories"]["injection"]["total"] == 20
    replay_calibration(report, report["report_sha256"])
    body = canonical_bytes(report)
    criterion = {
        "parameters": {
            "report_ref": "tenants/acme/judge.json",
            "report_object_sha256": sha256(body),
            "report_sha256": report["report_sha256"],
        }
    }
    services = type("Services", (), {"object_body": staticmethod(lambda _key: body)})()
    verifier = default_verifiers().get("verify_engineering_judge_calibration", 1)
    verified = verifier.handler(criterion, {"tenant_id": "acme"}, {}, services)
    assert verified.status == "passed"
    assert verified.summary["provider_provenance_verified"] is False
    assert (
        verifier.handler(criterion, {"tenant_id": "other"}, {}, services).error_code
        == "judge_report_scope_mismatch"
    )
    partial = calibration_report(suite, outputs[1:], **kwargs)
    assert partial["calibration_decision"] == "NO_GO"
    assert partial["metrics"]["total"] == 200
    assert partial["metrics"]["statuses"]["invalid"] == 1
    assert partial["metrics"]["positive_acceptance"]["denominator"] == 100
    tampered = deepcopy(report)
    tampered["metrics"]["total"] = 199
    with pytest.raises(ValueError, match="judge_report_hash_mismatch"):
        replay_calibration(tampered, report["report_sha256"])
    tampered["report_sha256"] = sha256(
        canonical_bytes({key: value for key, value in tampered.items() if key != "report_sha256"})
    )
    with pytest.raises(ValueError, match="judge_report_replay_mismatch"):
        replay_calibration(tampered, tampered["report_sha256"])
    for bad_outputs in (outputs + outputs[:1], [{"case_id": "unknown", "raw": "{}"}]):
        with pytest.raises(ValueError, match="judge_case_unknown_or_duplicate"):
            calibration_report(suite, bad_outputs, **kwargs)


def test_critical_false_acceptance_blocks_despite_good_aggregate_rate():
    suite = build_suite()
    outputs = []
    for case in suite["splits"]["judge_calibration"]["cases"]:
        result = scripted(case)
        if case["case_id"] == "judge_calibration-007-fail":
            result.update(question_covered=True, abstention_appropriate=True)
        outputs.append({"case_id": case["case_id"], "raw": json.dumps(result)})
    report = calibration_report(
        suite, outputs, expected_suite_sha256=suite["suite_sha256"], model=MODEL, prompt="test"
    )
    assert report["gates"]["negative_acceptance"] is True
    assert report["gates"]["critical_negative_acceptance"] is False
    assert report["calibration_decision"] == "NO_GO"
