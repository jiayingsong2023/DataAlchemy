import pytest

from src.harness.calibration import CalibrationPolicy, build_calibration_report


def cases():
    return [
        {"category": "security", "human_label": "fail", "judge_label": "fail"},
        {"category": "acl", "human_label": "pass", "judge_label": "pass"},
        {"category": "general", "human_label": "pass", "judge_label": "pass"},
    ]


def test_calibration_report_passes_only_with_hard_gate_coverage():
    report = build_calibration_report(cases(), CalibrationPolicy("h6-policy", min_cases=3))
    assert report["passed"] is True
    assert report["agreement"] == 1.0
    assert report["category_counts"]["security"] == 1


def test_calibration_rejects_false_accept_and_missing_labels():
    bad = cases()
    bad[0] = {"category": "security", "human_label": "fail", "judge_label": "pass"}
    report = build_calibration_report(bad, CalibrationPolicy("h6-policy", min_cases=3))
    assert report["passed"] is False
    assert report["false_accepts"] == 1
    with pytest.raises(ValueError, match="calibration_label_invalid"):
        build_calibration_report([{"human_label": "pass"}], CalibrationPolicy("h6-policy"))
