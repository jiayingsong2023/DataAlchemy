import hashlib

import pytest

from src.harness.deployment import DeploymentBinding, route_request, validate_shadow_output


def binding(mode="shadow", percent=0):
    salt = "pilot-salt"
    return DeploymentBinding(
        stable_release_id="stable", candidate_release_id="candidate",
        stable_digest="a" * 64, candidate_digest="b" * 64,
        stable_service="stable-svc", candidate_service="candidate-svc",
        mode=mode, canary_percent=percent, salt_sha256=hashlib.sha256(salt.encode()).hexdigest(),
    )


def test_shadow_keeps_stable_authority():
    assert route_request(binding(), "t1", "u1") == "stable"
    validate_shadow_output({"authority": "stable", "side_effects": []})


def test_canary_is_deterministic_and_shadow_is_read_only():
    b = binding("canary", 50)
    assert route_request(b, "t1", "u1") == route_request(b, "t1", "u1")
    with pytest.raises(ValueError, match="side_effects"):
        validate_shadow_output({"authority": "stable", "side_effects": ["write"]})


def test_binding_rejects_same_release():
    with pytest.raises(ValueError, match="release_pair"):
        DeploymentBinding("same", "same", "a" * 64, "b" * 64, "s", "c")
