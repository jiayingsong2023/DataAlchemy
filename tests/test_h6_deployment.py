import hashlib
from pathlib import Path

import pytest

from src.harness.deployment import DeploymentBinding, route_request, validate_shadow_output


def binding(mode="shadow", percent=0):
    salt = "pilot-salt"
    return DeploymentBinding(
        stable_release_id="stable",
        candidate_release_id="candidate",
        stable_digest="a" * 64,
        candidate_digest="b" * 64,
        stable_service="stable-svc",
        candidate_service="candidate-svc",
        mode=mode,
        canary_percent=percent,
        salt_sha256=hashlib.sha256(salt.encode()).hexdigest(),
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


def test_gpu_deployment_gate_executes_real_fp16_work():
    gate = Path("scripts/setup/verify_gpu.sh").read_text()

    assert 'dtype=torch.float16, device="cuda"' in gate
    assert "x @ x" in gate
    assert "torch.cuda.synchronize()" in gate
