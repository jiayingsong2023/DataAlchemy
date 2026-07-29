from src.config import MODEL_DIR, get_model_config


def test_model_config_expands_project_model_directory():
    model_b = get_model_config("model_b")

    assert model_b["model_path"] == f"{MODEL_DIR}/bge-small-zh-v1.5"
    assert model_b["reranker_path"] == f"{MODEL_DIR}/bge-reranker-base"
