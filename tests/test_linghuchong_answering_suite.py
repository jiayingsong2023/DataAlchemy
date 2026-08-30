"""Keep the PDF answering regression contract deterministic and reviewable."""

import hashlib
import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from src.rag.answering import local_evidence_answer

ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = ROOT / "tests/fixtures/linghuchong_answering_suite.json"


def _suite() -> dict:
    return json.loads(SUITE_PATH.read_text(encoding="utf-8"))


def test_linghuchong_answering_suite_has_grounded_and_abstention_cases():
    suite = _suite()
    assert suite["version"] == "linghuchong-answering-v1"
    assert suite["source"]["pages"] == 7

    cases = suite["cases"]
    assert len({case["case_id"] for case in cases}) == len(cases)
    assert {case["expected_status"] for case in cases} == {"grounded", "abstained"}

    for case in cases:
        assert case["query"].strip()
        if case["expected_status"] == "grounded":
            assert case["required_substrings"]
            assert case["required_pages"]
        else:
            assert case["expected_answer"] == "现有文档没有说明这个问题。"
            assert case["expected_citation_count"] == 0


def test_linghuchong_source_hash_matches_when_local_pdf_is_available():
    suite = _suite()
    source = ROOT / suite["source"]["path"]
    if not source.is_file():
        pytest.skip("The private PDF fixture is not present in this checkout")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == suite["source"]["sha256"]


def test_local_fallback_meets_linghuchong_regression_suite_when_pdf_is_available():
    suite = _suite()
    source = ROOT / suite["source"]["path"]
    if not source.is_file():
        pytest.skip("The private PDF fixture is not present in this checkout")
    context = [{"text": page.extract_text() or ""} for page in PdfReader(str(source)).pages]

    for case in suite["cases"]:
        answer = local_evidence_answer(case["query"], context)
        if case["expected_status"] == "grounded":
            assert all(term in answer for term in case["required_substrings"]), case["case_id"]
        else:
            assert answer == case["expected_answer"], case["case_id"]
