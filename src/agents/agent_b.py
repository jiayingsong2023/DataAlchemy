import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

import torch
import torch.distributed

from config import DATABASE_URL, get_model_config
from inference.batch_engine import BatchInferenceEngine
from inference.model_manager import ModelManager
from storage.postgres import PostgresDatabase
from utils.cloud_audit import observable_model_call
from utils.logger import logger
from utils.s3_utils import S3Utils

# Monkeypatch for ROCm Windows compatibility
if not hasattr(torch.distributed, "tensor"):

    class Dummy:
        pass

    torch.distributed.tensor = Dummy()
    torch.distributed.tensor.DTensor = Dummy


_PROTOCOL_MARKERS = ("### Instruction:", "### Response:")


def _clean_model_response(response: str) -> str:
    """Drop prompt-template leakage; an invalid Unicode completion is unusable evidence."""
    if "\ufffd" in response:
        logger.warning("Discarding LoRA completion containing replacement characters")
        return ""
    if "### Response:" in response:
        response = response.split("### Response:", 1)[1]
    for marker in _PROTOCOL_MARKERS:
        response = response.split(marker, 1)[0]
    return response.strip()


class AgentB:
    """Agent B: The Model Specialist (LoRA) - Optimized for AMD GPU."""

    def __init__(self, model_id: str = None, adapter_path: str = None):
        model_c = get_model_config("model_c")
        # Priority: model_path > model_id
        self.model_id = (
            model_id
            or model_c.get("model_path")
            or model_c.get("model_id", "TinyLlama/TinyLlama-1.1B-intermediate-step-1431k-3T")
        )
        self.adapter_path = adapter_path or model_c.get("adapter_path", "./lora-tiny-llama-adapter")

        # Initialize ModelManager and BatchEngine
        self.model_manager = ModelManager()
        self.batch_engine = None
        self.last_sync_time = 0
        self.last_adapter_id = None
        self.last_release_id = None
        self.last_artifact_sha256 = None

    def _ensure_engine(self, identity=None):
        """Ensure model is loaded and engine is initialized."""
        # The model process carries one adapter.  Recheck the H5 tenant/release
        # boundary for every request so a warmed engine cannot serve its adapter
        # to another tenant.
        self.check_and_reload_adapter(force=self.batch_engine is None, identity=identity)
        if self.batch_engine is None:
            self.batch_engine = BatchInferenceEngine(
                model_manager=self.model_manager, max_batch_size=4, max_wait_ms=50
            )

    def _promoted_adapter(self, identity):
        """Resolve one exact, promoted H5 adapter; never select the newest object."""
        if not identity or os.getenv("H5_LORA_MODE", "disabled") != "single_tenant_lora":
            return None
        release_tenant = os.getenv("MODEL_RELEASE_TENANT_ID")
        if not release_tenant or identity.get("tenant_id") != release_tenant:
            raise PermissionError("LoRA is disabled for this tenant")
        try:
            with PostgresDatabase(DATABASE_URL).transaction(identity, read_only=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT r.release_id, r.manifest_json, a.adapter_id, a.artifact_key, a.artifact_sha256, "
                        "a.artifact_size, a.base_model_digest, a.state "
                        "FROM release_records r JOIN adapter_manifests a ON a.adapter_id = r.adapter_id "
                        "WHERE r.tenant_id = %s AND r.status = 'promoted' "
                        "AND r.release_scope = 'single_tenant_lora' "
                        "ORDER BY r.updated_at DESC LIMIT 1",
                        (identity["tenant_id"],),
                    )
                    row = cursor.fetchone()
            if row is None or row["state"] != "verified":
                return None
            return row
        except Exception as error:
            logger.error(f"Unable to resolve promoted adapter: {error}")
            raise RuntimeError("promoted_adapter_unavailable") from error

    def _download_exact_adapter(self, row, s3):
        prefix = row["artifact_key"]
        staging = f"{self.adapter_path}.staging"
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        if not s3.download_directory(prefix, staging):
            raise RuntimeError("adapter_artifact_missing")
        files = sorted(path for path in Path(staging).rglob("*") if path.is_file())
        if not files:
            raise RuntimeError("adapter_artifact_empty")
        digest = hashlib.sha256()
        total = 0
        for path in files:
            body = path.read_bytes()
            digest.update(path.relative_to(staging).as_posix().encode())
            digest.update(body)
            total += len(body)
        if digest.hexdigest() != row["artifact_sha256"] or total != row["artifact_size"]:
            raise RuntimeError("adapter_artifact_hash_mismatch")
        return staging

    def model_status(self, identity=None):
        row = self._promoted_adapter(identity)
        return {
            "tenant_id": identity.get("tenant_id") if identity else None,
            "release_scope": "single_tenant_lora",
            "base_model_digest": row["base_model_digest"] if row else self.model_id,
            "adapter_id": self.last_adapter_id,
            "adapter_artifact_sha256": self.last_artifact_sha256,
            "release_id": self.last_release_id,
            "loaded": self.model_manager.base_model is not None,
            "loaded_at": getattr(self, "loaded_at", None),
        }

    def check_and_reload_adapter(self, force=False, identity=None, expected_release_id=None):
        """
        Check S3 for newer adapter weights and reload if necessary.
        """
        s3 = S3Utils()
        try:
            row = self._promoted_adapter(identity)
            if row is None:
                if force and not self.model_manager.base_model:
                    self.model_manager.load_models(self.model_id, compile_model=True)
                return False
            release_id = str(row["release_id"])
            if expected_release_id and release_id != expected_release_id:
                raise PermissionError("requested_release_not_active_for_tenant")
            if not force and self.last_sync_time == release_id:
                return False
            staging = self._download_exact_adapter(row, s3)
            if self.model_manager.base_model:
                loaded = self.model_manager.reload_lora_adapter(staging)
            else:
                self.model_manager.load_models(
                    base_model_id=self.model_id,
                    lora_adapter_path=staging,
                    compile_model=True,
                )
                loaded = True
            if loaded:
                self.last_sync_time = release_id
                self.last_release_id = release_id
                self.last_adapter_id = str(row.get("adapter_id")) if row.get("adapter_id") else None
                self.last_artifact_sha256 = row["artifact_sha256"]
                import datetime

                self.loaded_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                return True
        except Exception as e:
            logger.error(f"Error checking/reloading adapter: {e}")
            if isinstance(e, PermissionError):
                raise
        return False

    async def predict_async(
        self,
        user_query: str,
        max_new_tokens: int = 128,
        cache_scope: str | None = None,
        identity: dict[str, str] | None = None,
        trace_recorder: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """Get 'intuition' from the fine-tuned model using async batch engine."""
        self._ensure_engine(identity)

        prompt = f"### Instruction:\n{user_query}\n\n### Response:\n"

        # Use batch engine for inference
        generation_config = {"max_new_tokens": max_new_tokens, "do_sample": False}
        started = time.perf_counter()
        try:
            full_response = await self.batch_engine.generate(
                prompt,
                **generation_config,
                cache_scope=cache_scope,
            )
        except Exception:
            if trace_recorder:
                trace_recorder(
                    observable_model_call(
                        component="agent_b.predict",
                        model=self.model_id,
                        messages=[{"role": "user", "content": prompt}],
                        response=None,
                        generation_config=generation_config,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        status="failed",
                        revision_or_digest=self.model_status(identity)["base_model_digest"],
                    )
                )
            raise
        response = _clean_model_response(full_response)
        if trace_recorder:
            trace_recorder(
                observable_model_call(
                    component="agent_b.predict",
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    response=response,
                    generation_config=generation_config,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    status="succeeded",
                    revision_or_digest=self.model_status(identity)["base_model_digest"],
                )
            )
        return response

    def predict(
        self, user_query: str, max_new_tokens: int = 128, identity: dict[str, str] | None = None
    ) -> str:
        """Synchronous wrapper for predict_async (for backward compatibility)."""
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        if loop.is_running():
            # This is tricky if called from an async context,
            # but Coordinator is currently sync.
            import nest_asyncio

            nest_asyncio.apply()

        return loop.run_until_complete(
            self.predict_async(user_query, max_new_tokens, identity=identity)
        )
