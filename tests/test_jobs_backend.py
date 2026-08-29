from kubernetes import client

from src.core.jobs import KubernetesJobBackend


class _API:
    def __init__(self):
        self.body = None

    def create_namespaced_job(self, _namespace, body):
        self.body = body
        return type("Created", (), {"metadata": type("Meta", (), {"uid": "uid"})()})()


def _job(kind):
    return {
        "run_id": "run",
        "task_id": "task",
        "step_id": "step",
        "job_id": "job",
        "external_name": "job-name",
        "kind": kind,
        "input_key": "runs/input.json",
        "input_sha256": "a" * 64,
        "result_key": "runs/result.json",
        "tenant_id": "acme",
    }


def test_gpu_devices_are_opt_in_for_model_jobs(monkeypatch):
    api = _API()
    monkeypatch.setenv("HARNESS_JOB_GPU_ENABLED", "true")
    monkeypatch.setenv("HARNESS_JOB_GPU_PRIVILEGED", "true")
    monkeypatch.setattr(KubernetesJobBackend, "_api", staticmethod(lambda: (api, client)))

    KubernetesJobBackend().submit(_job("model_evaluate"))

    pod = api.body.spec.template.spec
    assert {volume.name for volume in pod.volumes} == {"kfd", "dri"}
    assert {mount.mount_path for mount in pod.containers[0].volume_mounts} == {
        "/dev/kfd",
        "/dev/dri",
    }
    security = pod.containers[0].security_context
    assert security.privileged is True
    assert security.seccomp_profile.type == "Unconfined"


def test_gpu_privilege_is_separate_opt_in(monkeypatch):
    api = _API()
    monkeypatch.setenv("HARNESS_JOB_GPU_ENABLED", "true")
    monkeypatch.delenv("HARNESS_JOB_GPU_PRIVILEGED", raising=False)
    monkeypatch.setattr(KubernetesJobBackend, "_api", staticmethod(lambda: (api, client)))

    KubernetesJobBackend().submit(_job("model_evaluate"))

    security = api.body.spec.template.spec.containers[0].security_context
    assert security.privileged is None
    assert security.allow_privilege_escalation is False
    assert security.seccomp_profile.type == "RuntimeDefault"


def test_gpu_can_mount_matching_host_rocm(monkeypatch):
    api = _API()
    monkeypatch.setenv("HARNESS_JOB_GPU_ENABLED", "true")
    monkeypatch.setenv("HARNESS_JOB_ROCM_HOST_PATH", "/opt/rocm-7.2.0")
    monkeypatch.setattr(KubernetesJobBackend, "_api", staticmethod(lambda: (api, client)))

    KubernetesJobBackend().submit(_job("model_evaluate"))

    pod = api.body.spec.template.spec
    assert {volume.name for volume in pod.volumes} == {"kfd", "dri", "rocm-host"}
    assert any(
        mount.name == "rocm-host" and mount.mount_path == "/opt/rocm" and mount.read_only
        for mount in pod.containers[0].volume_mounts
    )


def test_compiled_training_receives_verifier_url_and_target_model_mount(monkeypatch):
    api = _API()
    monkeypatch.setenv("HARNESS_JOB_MODEL_HOST_PATH", "/data/models/Qwen")
    monkeypatch.setenv("HARNESS_JOB_MODEL_CONTAINER_PATH", "/app/data/models/Qwen")
    monkeypatch.setenv("HARNESS_JOB_VERIFIER_DATABASE_URL", "postgresql://verifier/db")
    monkeypatch.setenv("HARNESS_JOB_CODE_HOST_PATH", "/data/dataalchemy-src")
    monkeypatch.setenv("H5_TRAIN_MAX_STEPS", "100")
    monkeypatch.setattr(KubernetesJobBackend, "_api", staticmethod(lambda: (api, client)))

    KubernetesJobBackend().submit(_job("lora_train"))

    container = api.body.spec.template.spec.containers[0]
    assert any(
        mount.name == "h5-base-model"
        and mount.mount_path == "/app/data/models/Qwen"
        and mount.read_only
        for mount in container.volume_mounts
    )
    assert any(
        mount.name == "harness-code" and mount.mount_path == "/app/src" and mount.read_only
        for mount in container.volume_mounts
    )
    assert next(env.value for env in container.env if env.name == "VERIFIER_DATABASE_URL") == (
        "postgresql://verifier/db"
    )
    assert next(env.value for env in container.env if env.name == "H5_TRAIN_MAX_STEPS") == "100"


def test_code_mount_does_not_require_a_model_mount(monkeypatch):
    api = _API()
    monkeypatch.setenv("HARNESS_JOB_CODE_HOST_PATH", "/data/dataalchemy-src")
    monkeypatch.delenv("HARNESS_JOB_MODEL_HOST_PATH", raising=False)
    monkeypatch.setattr(KubernetesJobBackend, "_api", staticmethod(lambda: (api, client)))

    KubernetesJobBackend().submit(_job("model_evaluate"))

    mounts = api.body.spec.template.spec.containers[0].volume_mounts
    assert {mount.name for mount in mounts} == {"harness-code"}


def test_spark_jobs_do_not_receive_gpu_devices(monkeypatch):
    api = _API()
    monkeypatch.delenv("HARNESS_JOB_GPU_ENABLED", raising=False)
    monkeypatch.setattr(KubernetesJobBackend, "_api", staticmethod(lambda: (api, client)))

    KubernetesJobBackend().submit(_job("spark_rough_clean"))

    pod = api.body.spec.template.spec
    assert pod.volumes == []
    assert pod.containers[0].volume_mounts == []
