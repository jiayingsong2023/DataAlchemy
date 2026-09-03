from src.harness.rag_projection_ab import aggregate, score_results


def test_rag_projection_scores_expected_page_and_content():
    case = {"required_pages": [2], "required_substrings": ["破气式", "火系魔素"]}
    score = score_results(
        [
            {"text": "unrelated", "metadata": {"locator": {"page": 1}}},
            {"text": "使用破气式控制火系魔素", "metadata": {"locator": {"page": 2}}},
        ],
        case,
    )

    assert score == {
        "recall": 1.0,
        "reciprocal_rank": 0.5,
        "context_coverage": 1.0,
        "citation_precision": 0.5,
        "returned_pages": [1, 2],
    }
    assert aggregate([score]) == {
        "recall": 1.0,
        "reciprocal_rank": 0.5,
        "context_coverage": 1.0,
        "citation_precision": 0.5,
    }
