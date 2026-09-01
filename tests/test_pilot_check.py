import pytest

from src.ops.pilot_check import check_environment


def test_pilot_check_rejects_missing_configuration():
    with pytest.raises(ValueError, match="DATABASE_URL, REDIS_URL, S3_ENDPOINT"):
        check_environment({})
