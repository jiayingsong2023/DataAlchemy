import json
import sys
from types import SimpleNamespace

from scripts import promote_tiered_release


def test_promote_cli_replays_decision_and_advances_release(monkeypatch, capsys):
    decision = {
        "policy": {
            "version": "tiered.v1",
            "normal_min_pass_rate": 0.9,
            "max_p95_regression_ratio": 1.2,
        },
        "base_repetitions": [{"p95_latency_ms": 100.0}],
        "candidate_repetitions": [
            {"normal": {"required": 100, "passed": 98}, "p95_latency_ms": 90.0} for _ in range(3)
        ],
    }
    stored = {}

    class Services:
        def __init__(self, *_):
            pass

        def object_body(self, _ref):
            return json.dumps(decision).encode()

    class Verifiers:
        def get(self, *_):
            return SimpleNamespace(
                handler=lambda *_: SimpleNamespace(
                    status="passed", summary={"status": "GO", "critical_passed": True}
                )
            )

    class Store:
        def __init__(self):
            self.bucket = "test"
            self.client = object()

        def put_object(self, ref, body, _content_type):
            stored[ref] = body
            return True

    class Governance:
        def __init__(self, _database_url):
            self.transitions = []
            monkeypatch.setattr(promote_tiered_release, "_test_governance", self, raising=False)

        def create_candidate(self, _identity, manifest):
            self.manifest = manifest
            return "release-1"

        def advance(self, _release_id, target, _identity):
            self.transitions.append(target)

        def observe(self, *_args, **_kwargs):
            self.transitions.append("promoted")
            return "promoted"

    monkeypatch.setattr(promote_tiered_release, "ReadOnlyServices", Services)
    monkeypatch.setattr(promote_tiered_release, "default_verifiers", Verifiers)
    monkeypatch.setattr(promote_tiered_release, "S3Utils", Store)
    monkeypatch.setattr(promote_tiered_release, "ReleaseGovernance", Governance)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "promote_tiered_release.py",
            "--adapter-id",
            "adapter-1",
            "--snapshot-id",
            "snapshot-1",
            "--decision-ref",
            "decision.json",
            "--decision-sha256",
            "a" * 64,
            "--tenant-id",
            "acme",
            "--database-url",
            "postgresql://release",
            "--verifier-database-url",
            "postgresql://verifier",
        ],
    )

    promote_tiered_release.main()

    governance = promote_tiered_release._test_governance
    assert governance.transitions == ["shadow", "canary", "promoted"]
    assert governance.manifest["release_decision"]["sha256"] == "a" * 64
    assert governance.manifest["guardrails"]["min_samples"] == 300
    assert len(stored) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "promoted"
