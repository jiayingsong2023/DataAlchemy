
from src.config import S3_BUCKET, get_model_config


def test_config_loading():
    """Test that configuration can be loaded."""
    # S3_BUCKET should be available
    assert S3_BUCKET is not None

def test_get_model_config():
    """Test retrieving model configuration."""
    # Test for a missing key
    config = get_model_config("non_existent_model")
    assert config == {}

    # Test for model_b (usually exists in models.yaml)
    config_b = get_model_config("model_b")
    assert isinstance(config_b, dict)
