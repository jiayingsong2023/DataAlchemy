import os
import hashlib
from pathlib import Path

import torch
import torch.distributed

from config import DATABASE_URL, get_model_config
from inference.batch_engine import BatchInferenceEngine
from inference.model_manager import ModelManager
from storage.postgres import PostgresDatabase
from utils.logger import logger
from utils.s3_utils import S3Utils

# Monkeypatch for ROCm Windows compatibility
if not hasattr(torch.distributed, "tensor"):
    class Dummy: pass
    torch.distributed.tensor = Dummy()
    torch.distributed.tensor.DTensor = Dummy

class AgentB:
    """Agent B: The Model Specialist (LoRA) - Optimized for AMD GPU."""

    def __init__(self, model_id: str = None, adapter_path: str = None):
        model_c = get_model_config("model_c")
        # Priority: model_path > model_id
        self.model_id = model_id or model_c.get("model_path") or model_c.get("model_id", "TinyLlama/TinyLlama-1.1B-intermediate-step-1431k-3T")
        self.adapter_path = adapter_path or model_c.get("adapter_path", "./lora-tiny-llama-adapter")

        # Initialize ModelManager and BatchEngine
        self.model_manager = ModelManager()
        self.batch_engine = None
        self.last_sync_time = 0

    def _ensure_engine(self, identity=None):
        """Ensure model is loaded and engine is initialized."""
        if self.batch_engine is None:
            self.check_and_reload_adapter(force=True, identity=identity)

            self.batch_engine = BatchInferenceEngine(
                model_manager=self.model_manager,
                max_batch_size=4,
                max_wait_ms=50
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
                        "SELECT r.release_id, r.manifest_json, a.artifact_key, a.artifact_sha256, "
                        "a.artifact_size, a.base_model_digest, a.state "
                        "FROM release_records r JOIN adapter_manifests a ON a.adapter_id = r.adapter_id "
                        "WHERE r.tenant_id = %s AND r.status = 'promoted' "
                        "AND r.release_scope = 'single_tenant_lora'",
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
        os.makedirs(self.adapter_path, exist_ok=True)
        if not s3.download_directory(prefix, self.adapter_path):
            raise RuntimeError("adapter_artifact_missing")
        files = sorted(path for path in Path(self.adapter_path).rglob("*") if path.is_file())
        if not files:
            raise RuntimeError("adapter_artifact_empty")
        digest = hashlib.sha256()
        total = 0
        for path in files:
            body = path.read_bytes()
            digest.update(path.relative_to(self.adapter_path).as_posix().encode())
            digest.update(body)
            total += len(body)
        if digest.hexdigest() != row["artifact_sha256"] or total != row["artifact_size"]:
            raise RuntimeError("adapter_artifact_hash_mismatch")

    def check_and_reload_adapter(self, force=False, identity=None):
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
            if not force and self.last_sync_time == release_id:
                return False
            self._download_exact_adapter(row, s3)
            if self.model_manager.base_model:
                loaded = self.model_manager.reload_lora_adapter(self.adapter_path)
            else:
                self.model_manager.load_models(
                    base_model_id=self.model_id,
                    lora_adapter_path=self.adapter_path,
                    compile_model=True,
                )
                loaded = True
            if loaded:
                self.last_sync_time = release_id
                return True
        except Exception as e:
            logger.error(f"Error checking/reloading adapter: {e}")
            if isinstance(e, PermissionError):
                raise
        return False

    async def predict_async(
        self, user_query: str, max_new_tokens: int = 128, cache_scope: str | None = None,
        identity: dict[str, str] | None = None,
    ) -> str:
        """Get 'intuition' from the fine-tuned model using async batch engine."""
        self._ensure_engine(identity)

        prompt = f"### Instruction:\n{user_query}\n\n### Response:\n"

        # Use batch engine for inference
        full_response = await self.batch_engine.generate(
            prompt,
            max_new_tokens=max_new_tokens,
            cache_scope=cache_scope,
        )

        if "### Response:" in full_response:
            return full_response.split("### Response:")[-1].strip()
        return full_response[len(prompt):].strip()

    def predict(self, user_query: str, max_new_tokens: int = 128, identity: dict[str, str] | None = None) -> str:
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

        return loop.run_until_complete(self.predict_async(user_query, max_new_tokens, identity=identity))
