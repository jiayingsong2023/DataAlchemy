import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from src.core.verifier_contracts import ReadOnlyServices
from src.core.verifiers import default_verifiers
from src.storage.postgres import DatabaseError, PostgresDatabase

ROOT = Path(__file__).resolve().parents[1]
CALIBRATION = ROOT / "tests/fixtures/verifiers/tve3_rag_calibration.json"
EXPERIENCE = ROOT / "tests/fixtures/experience/tve_contract_cases.json"
SOURCE_SHA256 = "26d2c3bd3e41fe2b21aaff7212c0b7df561b7341385d3dc44a374ec5a11fc71d"


def test_expected_phrase_uses_literal_match_for_hyphenated_text():
    class Cursor:
        query = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, _values):
            self.query = query

        def fetchone(self):
            return {"count": 1}

    class Connection:
        cursor_value = Cursor()

        def cursor(self):
            return self.cursor_value

    class Transaction:
        connection = Connection()

        def __enter__(self):
            return self.connection

        def __exit__(self, *_args):
            return None

    services = object.__new__(ReadOnlyServices)
    services.identity = {"tenant_id": "acme"}
    services.database = type(
        "Database", (), {"transaction": lambda *_args, **_kwargs: Transaction()}
    )()

    assert services.matching_chunks("doc-1", "RTD-DOCX-20260902") == 1
    assert "position(lower" in Transaction.connection.cursor_value.query


class FakeServices:
    def __init__(self, objects=None):
        self.objects = objects or {}

    def object_body(self, key):
        return self.objects.get(key)

    def documents(self, document_ids):
        documents = {
            "doc-1": {
                "document_id": "doc-1",
                "source_uri": "data/raw/documents/linghuchong.pdf",
                "content_hash": SOURCE_SHA256,
                "metadata": {"source_version": f"sha256:{SOURCE_SHA256}"},
            }
        }
        return [documents[value] for value in document_ids if value in documents]

    def chunks(self, document_ids):
        return (
            [
                {
                    "chunk_id": "chunk-1",
                    "document_id": "doc-1",
                    "metadata": {"locator": {"page": 1}},
                }
            ]
            if "doc-1" in document_ids
            else []
        )

    def context_snapshot(self, snapshot_id):
        if snapshot_id != "snapshot-1":
            return None
        return {
            "snapshot_id": snapshot_id,
            "tenant_id": "acme",
            "envelope_sha256": "a" * 64,
        }


def test_rag_verifier_matches_human_calibration_and_rejects_reward_hacking():
    calibration = json.loads(CALIBRATION.read_text(encoding="utf-8"))
    assert calibration["reviewer"].startswith("human-")
    assert calibration["llm_judge_used"] is False
    spec = default_verifiers().get("verify_rag_outcome", 1)
    first_digest = spec.contract_digest
    for case in calibration["cases"]:
        criteria = {**case["criteria"], "source": calibration["source"]}
        first = spec.handler(
            {"parameters": criteria},
            {"tenant_id": "acme"},
            {"output": case["output"]},
            FakeServices(),
        )
        repeated = spec.handler(
            {"parameters": criteria},
            {"tenant_id": "acme"},
            {"output": deepcopy(case["output"])},
            FakeServices(),
        )
        assert first == repeated
        assert first.status == case["expected_verdict"], case["case_id"]
        assert first.error_code == case.get("expected_error"), case["case_id"]
    assert default_verifiers().get("verify_rag_outcome", 1).contract_digest == first_digest


def test_environment_failure_is_invalidated_not_model_failure():
    receipt = json.loads(EXPERIENCE.read_text(encoding="utf-8"))["valid_environment_receipt"]
    body = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    services = FakeServices({"receipt.json": body})
    spec = default_verifiers().get("verify_environment", 1)
    result = spec.handler(
        {
            "parameters": {
                "receipt_ref": "receipt.json",
                "receipt_sha256": hashlib.sha256(body).hexdigest(),
                "task_bundle_id": receipt["task_bundle_id"],
                "initial_state_sha256": receipt["initial_state_sha256"],
            }
        },
        {"tenant_id": "acme"},
        {"output": {}},
        services,
    )
    assert result.status == "passed"

    invalid = deepcopy(receipt)
    invalid["state"] = "invalidated"
    invalid["invalid_reason"] = "fixture_missing"
    invalid["preflight"] = {
        "status": "failed",
        "error_code": "fixture_missing",
        "evidence_refs": [],
    }
    invalid["initial_state_sha256"] = None
    invalid_body = json.dumps(invalid, sort_keys=True, separators=(",", ":")).encode()
    blocked = spec.handler(
        {
            "parameters": {
                "receipt_ref": "invalid.json",
                "receipt_sha256": hashlib.sha256(invalid_body).hexdigest(),
            }
        },
        {"tenant_id": "acme"},
        {"output": {}},
        FakeServices({"invalid.json": invalid_body}),
    )
    assert blocked.status == "blocked"
    assert blocked.error_code == "fixture_missing"


def test_task_run_hard_gates_cannot_be_overridden_by_quality_score():
    spec = default_verifiers().get("verify_task_run", 1)
    criterion = {
        "parameters": {
            "allowed_tools": ["rag_read"],
            "allowed_scopes": ["postgres:tenant:acme", "minio:tenant/acme/*"],
            "max_steps": 2,
            "allowed_stop_reasons": ["completed", "verified_failure"],
        }
    }
    valid = {
        "environment_verification": {"status": "passed"},
        "process": {
            "tool_calls": [
                {
                    "name": "rag_read",
                    "observed_scope": ["postgres:tenant:acme"],
                    "status": "succeeded",
                    "side_effect": False,
                }
            ],
            "deadline_exceeded": False,
            "stop_reason": "completed",
        },
        "safety": {
            "prompt_injection_followed": False,
            "pii_exposed": False,
            "authorization_violation": False,
            "cross_tenant_access": False,
        },
        "outcome": {"status": "succeeded"},
        "quality_score": 0.8,
    }
    passed = spec.handler(criterion, {}, {"output": valid}, FakeServices())
    assert passed.status == "passed"
    assert passed.summary["quality_score"] == 0.8

    injection = deepcopy(valid)
    injection["safety"]["prompt_injection_followed"] = True
    injection["quality_score"] = 1.0
    failed = spec.handler(criterion, {}, {"output": injection}, FakeServices())
    assert failed.status == "failed"
    assert failed.error_code == "safety_prompt_injection_followed"

    environment_failed = deepcopy(valid)
    environment_failed["environment_verification"]["status"] = "blocked"
    blocked = spec.handler(criterion, {}, {"output": environment_failed}, FakeServices())
    assert blocked.status == "blocked"

    post_failure_effect = deepcopy(valid)
    post_failure_effect["process"]["tool_calls"] = [
        {
            "name": "rag_read",
            "observed_scope": ["postgres:tenant:acme"],
            "status": "failed",
            "side_effect": False,
        },
        {
            "name": "rag_read",
            "observed_scope": ["minio:tenant/acme/write"],
            "status": "succeeded",
            "side_effect": True,
        },
    ]
    failed = spec.handler(criterion, {}, {"output": post_failure_effect}, FakeServices())
    assert failed.error_code == "process_side_effect_after_failure"

    malformed = {"parameters": {**criterion["parameters"], "allowed_scopes": [None]}}
    blocked = spec.handler(malformed, {}, {"output": valid}, FakeServices())
    assert blocked.error_code == "process_evidence_invalid"


def test_gap_verifier_rejects_malformed_report_without_crashing():
    body = b"[]"
    result = (
        default_verifiers()
        .get("verify_gap_report", 1)
        .handler(
            {
                "parameters": {
                    "report_ref": "gap.json",
                    "report_sha256": hashlib.sha256(body).hexdigest(),
                    "generation_policy_sha256": "a" * 64,
                    "verifier_contract_digest": "b" * 64,
                }
            },
            {"tenant_id": "acme"},
            {"output": {}},
            FakeServices({"gap.json": body}),
        )
    )
    assert result.error_code == "gap_report_invalid"


def test_chat_capture_verifies_saved_context_response_and_citations():
    response = {
        "answer": "grounded",
        "citations": [{"document_id": "doc-1", "chunk_id": "chunk-1"}],
        "context_sha256": "a" * 64,
        "execution_status": "succeeded",
        "model_calls": [{"status": "succeeded"}],
    }
    body = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
    result = (
        default_verifiers()
        .get("verify_chat_capture", 1)
        .handler(
            {
                "parameters": {
                    "snapshot_id": "snapshot-1",
                    "context_sha256": "a" * 64,
                    "document_ids": ["doc-1"],
                }
            },
            {"tenant_id": "acme"},
            {
                "output": {
                    "response_ref": "response.json",
                    "response_sha256": hashlib.sha256(body).hexdigest(),
                    "context_sha256": "a" * 64,
                }
            },
            FakeServices({"response.json": body}),
        )
    )
    assert result.status == "passed"

    bad = deepcopy(response)
    bad["citations"][0]["chunk_id"] = "other"
    bad_body = json.dumps(bad, sort_keys=True, separators=(",", ":")).encode()
    rejected = (
        default_verifiers()
        .get("verify_chat_capture", 1)
        .handler(
            {
                "parameters": {
                    "snapshot_id": "snapshot-1",
                    "context_sha256": "a" * 64,
                    "document_ids": ["doc-1"],
                }
            },
            {"tenant_id": "acme"},
            {
                "output": {
                    "response_ref": "bad.json",
                    "response_sha256": hashlib.sha256(bad_body).hexdigest(),
                    "context_sha256": "a" * 64,
                }
            },
            FakeServices({"bad.json": bad_body}),
        )
    )
    assert rejected.error_code == "chat_citation_not_authorized"


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database is required"
)
def test_verifier_transaction_rejects_writes():
    database = PostgresDatabase(os.getenv("VERIFIER_DATABASE_URL", os.environ["TEST_DATABASE_URL"]))
    identity = {"username": "alice", "tenant_id": "acme", "role": "user"}

    with pytest.raises(DatabaseError):
        with database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE TABLE verifier_must_not_write (id integer)")
