"""The v2 verifier is independent of the answer producer and keeps v1 replay."""

import hashlib
import json

import pytest

from core.verifiers import default_verifiers


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _fixture():
    text = "Orion uses port 8080."
    row = {"document_id": "doc", "chunk_id": "chunk", "context_type": "document", "text": text}
    envelope = {
        "schema_version": "context-envelope.v1",
        "snapshot_id": "snapshot",
        "identity": {"tenant_id": "acme"},
        "retrieval_context": [row],
    }
    citation = {
        "document_id": "doc",
        "chunk_id": "chunk",
        "source_uri": None,
        "source_version": None,
        "source_sha256": "",
        "locator": None,
        "source_span_ids": [],
        "source_content_sha256": None,
        "acl_digest": None,
        "chunk_policy_version": None,
        "quote": text,
        "quote_start": 0,
        "quote_end": len(text),
    }
    response = {
        "schema_version": "rag_chat_response.v2",
        "answer": "根据文档：" + text,
        "answer_status": "answered",
        "answer_mode": "extractive",
        "support_status": "extract_verified",
        "reason_code": None,
        "citations": [citation],
        "execution_status": "succeeded",
        "model_calls": [],
    }
    return envelope, response


def _verify(
    envelope, response, *, altered_chunk=False, ready=True, version=2, corrupt_context=False
):
    digest = hashlib.sha256(_bytes(envelope)).hexdigest()
    envelope = {**envelope, "envelope_sha256": digest}
    response = {**response, "context_sha256": digest}
    context_ref, response_ref = "tenants/acme/context", "tenants/acme/response"
    objects = {context_ref: _bytes(envelope), response_ref: _bytes(response)}
    parameters = {
        "snapshot_id": "snapshot",
        "context_sha256": digest,
        "document_ids": ["doc"],
        "context_ref": context_ref,
        "context_object_sha256": hashlib.sha256(objects[context_ref]).hexdigest(),
    }
    if not envelope["retrieval_context"]:
        parameters["document_ids"] = []
    if corrupt_context:
        objects[context_ref] += b" "

    class Services:
        object_body = staticmethod(objects.get)

        @staticmethod
        def context_snapshot(_snapshot_id):
            return {"tenant_id": "acme", "envelope_sha256": digest}

        @staticmethod
        def documents(ids):
            return [
                {"document_id": item, "status": "ready" if ready else "revoked"} for item in ids
            ]

        @staticmethod
        def chunks(ids):
            return [
                {
                    "document_id": item,
                    "chunk_id": "chunk",
                    "text": "changed" if altered_chunk else "Orion uses port 8080.",
                }
                for item in ids
            ]

    return (
        default_verifiers()
        .get("verify_chat_capture", version)
        .handler(
            {"parameters": parameters},
            {"tenant_id": "acme"},
            {
                "output": {
                    "response_ref": response_ref,
                    "response_sha256": hashlib.sha256(objects[response_ref]).hexdigest(),
                    "context_ref": context_ref,
                    "context_sha256": digest,
                }
            },
            Services(),
        )
    )


def test_chat_v2_extract_and_generated_have_no_semantic_quality_claim():
    envelope, response = _fixture()
    verified = _verify(envelope, response)
    assert verified.status == "passed"
    assert verified.summary["independent_semantic_verification"] is False
    assert "quality_score" not in verified.summary
    response.update(
        answer="The port is 8080.",
        answer_mode="generated",
        support_status="not_semantically_verified",
    )
    assert _verify(envelope, response).status == "passed"


@pytest.mark.parametrize(
    "change",
    [
        {"answer": "根据文档：Orion uses port 9999."},
        {"answer_status": "unknown"},
        {"answer_mode": []},
        {"support_status": "not_applicable"},
        {"reason_code": "insufficient_support"},
        {"execution_status": "failed"},
        {"model_calls": [{"status": "failed"}]},
        {"model_calls": {}},
        {"citations": [{"chunk_id": []}]},
    ],
)
def test_chat_v2_rejects_invalid_answer_contract(change):
    envelope, response = _fixture()
    response.update(change)
    assert _verify(envelope, response).status != "passed"


@pytest.mark.parametrize(
    "change",
    [
        {"quote": "forged"},
        {"quote_start": True},
        {"quote_end": 999},
        {"source_uri": "forged"},
        {"acl_digest": "forged"},
        {"chunk_id": "other"},
        {"source_span_ids": ["other"]},
        {"source_version": "other"},
    ],
)
def test_chat_v2_rejects_forged_citations(change):
    envelope, response = _fixture()
    response["citations"][0].update(change)
    assert _verify(envelope, response).status != "passed"


def test_chat_v2_checks_current_document_text_status_and_object_hash():
    envelope, response = _fixture()
    for options in ({"altered_chunk": True}, {"ready": False}, {"corrupt_context": True}):
        assert _verify(envelope, response, **options).status != "passed"


def test_chat_v2_abstention_is_structured_and_v1_replay_is_unchanged():
    envelope, response = _fixture()
    response.update(
        answer="现有文档没有说明这个问题。",
        citations=[],
        answer_status="abstained",
        support_status="not_applicable",
        reason_code="insufficient_support",
    )
    assert _verify(envelope, response).status == "passed"
    envelope["retrieval_context"] = []
    assert _verify(envelope, response).status != "passed"
    response["reason_code"] = "no_document_evidence"
    assert _verify(envelope, response).status == "passed"
    response = {
        key: value
        for key, value in response.items()
        if key
        not in {"schema_version", "answer_status", "answer_mode", "support_status", "reason_code"}
    }
    assert _verify(envelope, response, version=1).status == "passed"
