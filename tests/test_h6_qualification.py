import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.core.verifiers import VerificationResult, default_verifiers
from src.harness.qualification import QualificationService, validate_qualification_manifest
from src.harness.rtd_q2_qualification import _pii_violations, evaluate_gates

Q0_MANIFEST = (
    Path(__file__).resolve().parents[1] / "docs" / "release" / "RTD_Q0_QUALIFICATION_MANIFEST.json"
)
Q0_SOURCE_MANIFEST = Q0_MANIFEST.with_name("RTD_Q0_SOURCE_MANIFEST.json")
Q0_SUITE = Q0_MANIFEST.with_name("RTD_Q0_QUALIFICATION_SUITE.json")


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
        "deployment_evidence_key": "qualifications/acme/deployment.json"
        if state == "pilot_ready"
        else None,
        "deployment_evidence_sha256": "d" * 64 if state == "pilot_ready" else None,
        "reason": None,
    }


class FakeVerifierServices:
    identity = {"tenant_id": "acme", "username": "verifier", "role": "reviewer"}

    def __init__(self, row, objects=None):
        self.row = row
        self.objects = objects or {"qualification/rtd-q0.json": Q0_MANIFEST.read_bytes()}

    def qualification(self, _qualification_id):
        return self.row

    def object_body(self, ref):
        return self.objects.get(ref)


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


def test_rtd_q0_manifest_is_valid_frozen_contract():
    manifest = json.loads(Q0_MANIFEST.read_text(encoding="utf-8"))

    assert validate_qualification_manifest(manifest) == manifest
    assert manifest["state"] == "frozen"
    assert (
        hashlib.sha256(Q0_SOURCE_MANIFEST.read_bytes()).hexdigest()
        == manifest["data_scope"]["source_manifest_sha256"]
    )
    assert hashlib.sha256(Q0_SUITE.read_bytes()).hexdigest() == manifest["suite"]["sha256"]

    frozen = deepcopy(manifest)
    frozen["blockers"] = ["late_threshold_change"]
    with pytest.raises(ValueError, match="qualification_manifest_not_ready"):
        validate_qualification_manifest(frozen)


def test_rtd_q0_manifest_can_be_independently_verified_as_frozen():
    body = Q0_MANIFEST.read_bytes()
    manifest = json.loads(body)
    objects = {
        "qualification/rtd-q0.json": body,
        manifest["data_scope"]["source_manifest_ref"]: Q0_SOURCE_MANIFEST.read_bytes(),
        manifest["suite"]["ref"]: Q0_SUITE.read_bytes(),
    }
    result = (
        default_verifiers()
        .get("verify_qualification_manifest", 1)
        .handler(
            {
                "parameters": {
                    "manifest_ref": "qualification/rtd-q0.json",
                    "manifest_sha256": hashlib.sha256(body).hexdigest(),
                    "expected_state": "frozen",
                }
            },
            {"tenant_id": "default"},
            {},
            FakeVerifierServices(qualification_row(), objects),
        )
    )

    assert result.status == "passed"
    assert result.summary["blockers"] == []

    objects[manifest["data_scope"]["source_manifest_ref"]] = b"tampered"
    failed = (
        default_verifiers()
        .get("verify_qualification_manifest", 1)
        .handler(
            {
                "parameters": {
                    "manifest_ref": "qualification/rtd-q0.json",
                    "manifest_sha256": hashlib.sha256(body).hexdigest(),
                    "expected_state": "frozen",
                }
            },
            {"tenant_id": "default"},
            {},
            FakeVerifierServices(qualification_row(), objects),
        )
    )
    assert failed.error_code == "qualification_source_manifest_hash_mismatch"


def test_rtd_q2_gate_evaluation_fails_a_hard_regression():
    manifest = {
        "gates": [
            {
                "name": "citation",
                "metric": "citation_precision",
                "operator": "gte_baseline",
                "value": None,
                "hard": True,
            }
        ]
    }

    [gate] = evaluate_gates(manifest, {"citation_precision": 0.8}, {"citation_precision": 0.9})

    assert gate["passed"] is False
    assert gate["target"] == 0.9


def test_rtd_q2_pii_probe_uses_qualification_tenant(monkeypatch):
    seen = {}

    class Database:
        def transaction(self, identity, read_only=False):
            seen.update(identity=identity, read_only=read_only)
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return self

        def execute(self, _query, parameters):
            seen["parameters"] = parameters

        def fetchall(self):
            return [{"text": "clean"}]

    class Store:
        database = Database()

    monkeypatch.setattr("src.harness.rtd_q2_qualification.VectorStore", lambda **_kwargs: Store())

    assert _pii_violations("document-1", "rtd-q3-isolated") == 0
    assert seen["identity"]["tenant_id"] == "rtd-q3-isolated"
    assert seen["read_only"] is True
    assert seen["parameters"] == ("document-1",)
