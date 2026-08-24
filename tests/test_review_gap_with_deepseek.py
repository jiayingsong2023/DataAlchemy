import pytest

from scripts.compile_sft_experiences import _format_messages
from scripts.review_gap_with_deepseek import accepted_judgments, candidate_tasks


def test_gap_review_selects_failed_targets_and_requires_two_grounded_passes():
    report = {
        "tasks": [
            {
                "case_id": "train-1",
                "classification": "weak",
                "outcomes": [
                    {"target_fingerprint_sha256": "tiny", "state": "succeeded"},
                    {"target_fingerprint_sha256": "qwen", "state": "failed"},
                ],
            },
            {
                "case_id": "holdout-1",
                "classification": "solved",
                "outcomes": [{"target_fingerprint_sha256": "qwen", "state": "succeeded"}],
            },
        ]
    }
    assert [item["case_id"] for item in candidate_tasks(report, {"qwen"})] == ["train-1"]
    case = {"case_id": "train-1", "required_substrings": ["gold answer"]}
    first = {
        "cases": [
            {
                "case_id": "train-1",
                "answerable": True,
                "expected_response": "The gold answer applies.",
                "confidence": 0.99,
            }
        ]
    }
    second = {
        "cases": [
            {
                "case_id": "train-1",
                "decision": "approved",
                "expected_response": "gold answer",
                "confidence": 0.98,
            }
        ]
    }
    assert accepted_judgments([case], first, second)["train-1"]
    second["cases"][0]["confidence"] = 0.9
    with pytest.raises(ValueError, match="deepseek_judgment_not_approved"):
        accepted_judgments([case], first, second)


def test_compiler_formats_models_without_chat_templates():
    tokenizer = type("Tokenizer", (), {"chat_template": None, "eos_token": "</s>"})()
    assert _format_messages(tokenizer, [{"role": "user", "content": "question"}]) == (
        "user: question</s>"
    )
