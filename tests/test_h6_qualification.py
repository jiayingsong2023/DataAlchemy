import pytest

from src.core.verifiers import VerificationResult, default_verifiers
from src.harness.qualification import QualificationService


def qualification_row(state="calibrated"):
    return {
        "qualification_id": "qualification-1",
        "tenant_id": "acme",
        "purpose": "pilot",
        "state": state,
        "data_owner": "owner",
        "created_by": "owner",
        "reviewer": "reviewer",
        "source_manifest_key": "qualifications/acme/manifest.json",
        "source_manifest_sha256": "a" * 64,
        "source_acl_digest": "acl-v1",
        "permission_version": "permission-v1",
        "data_classification": "internal",
        "suite_version": "suite-v1",
        "suite_sha256": "b" * 64,
        "policy_version": "policy-v1",
        "base_evaluation_id": "base-1",
        "candidate_evaluation_id": "candidate-1",
        "calibration_report_key": "qualifications/acme/calibration.json",
        "calibration_report_sha256": "c" * 64,
        "stable_release_id": "stable-1" if state == "pilot_ready" else None,
        "candidate_release_id": "candidate-1" if state == "pilot_ready" else None,
        "deployment_evidence_key": "qualifications/acme/deployment.json" if state == "pilot_ready" else None,
        "deployment_evidence_sha256": "d" * 64 if state == "pilot_ready" else None,
        "reason": None,
    }


class FakeVerifierServices:
    identity = {"tenant_id": "acme", "username": "verifier", "role": "reviewer"}

    def __init__(self, row):
        self.row = row

    def qualification(self, _qualification_id):
        return self.row


def test_qualification_verifier_requires_expected_state_and_provenance():
    verifier = default_verifiers().get("verify_qualification", 1)
    result = verifier.handler(
        {"parameters": {"qualification_id": "qualification-1", "expected_state": "calibrated"}},
        {},
        {},
        FakeVerifierServices(qualification_row()),
    )
    assert isinstance(result, VerificationResult)
    assert result.status == "passed"

    blocked = verifier.handler(
        {"parameters": {"qualification_id": "qualification-1", "expected_state": "pilot_ready"}},
        {},
        {},
        FakeVerifierServices(qualification_row()),
    )
    assert blocked.status == "failed"
    assert blocked.error_code == "qualification_state_mismatch"


def test_qualification_verifier_rejects_creator_as_reviewer():
    row = qualification_row()
    row["reviewer"] = row["created_by"]
    verifier = default_verifiers().get("verify_qualification", 1)
    result = verifier.handler(
        {"parameters": {"qualification_id": "qualification-1", "expected_state": "calibrated"}},
        {},
        {},
        FakeVerifierServices(row),
    )
    assert result.status == "failed"
    assert result.error_code == "qualification_calibration_incomplete"


def test_qualification_create_rejects_invalid_hash_before_database():
    service = QualificationService("")
    with pytest.raises(ValueError, match="source_manifest_sha256_invalid"):
        service.create(
            {"tenant_id": "acme", "username": "owner", "role": "user"},
            purpose="pilot",
            source_manifest_key="qualifications/acme/manifest.json",
            source_manifest_sha256="bad",
            source_acl_digest="acl-v1",
            permission_version="permission-v1",
            data_classification="internal",
            suite_version="suite-v1",
            suite_sha256="b" * 64,
            policy_version="policy-v1",
        )
