"""DataAlchemy FastAPI application bootstrap."""

import logging
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from config import validate_config
from utils.logger import logger
from utils.user_db import init_user_db
from webui import state
from webui.generate_cert import generate_self_signed_cert
from webui.routes.chat_tasks import router as chat_tasks_router
from webui.routes.data_release import router as data_release_router
from webui.routes.memory_feedback import router as memory_feedback_router


class LogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return "10054" not in message and not ("/api/status" in message and "404" in message)


logging.getLogger("uvicorn.error").addFilter(LogFilter())
logging.getLogger("uvicorn.access").addFilter(LogFilter())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    webui_dir = os.path.dirname(os.path.abspath(__file__))
    cert_path = os.path.join(webui_dir, "cert.pem")
    key_path = os.path.join(webui_dir, "key.pem")
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        try:
            logger.info("Certificates not found. Generating self-signed certificates...")
            generate_self_signed_cert(cert_path, key_path)
        except Exception as error:
            logger.warning(
                "Failed to generate certificates: %s. HTTPS may not be available.", error
            )
    validate_config()
    init_user_db()
    yield
    logger.info("Shutting down and releasing resources...")
    try:
        if state._adapter_runtime.batch_engine is not None:
            await state._adapter_runtime.batch_engine.shutdown()
        state._adapter_runtime.model_manager.clear_cache()
    except Exception as error:
        logger.error("Error during cleanup: %s", error)
    finally:
        logger.info("Shutting down. Releasing GPU resources...")
        sys.stdout.flush()
        if os.getenv("FORCE_EXIT", "true").lower() == "true":
            os._exit(0)


app = FastAPI(title="DataAlchemy WebUI", lifespan=lifespan)


@app.get("/metrics")
async def metrics():
    logger.info("Metrics endpoint hit")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


app.include_router(chat_tasks_router)
app.include_router(data_release_router)
app.include_router(memory_feedback_router)

static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


if __name__ == "__main__":
    webui_dir = os.path.dirname(os.path.abspath(__file__))
    cert_path = os.path.join(webui_dir, "cert.pem")
    key_path = os.path.join(webui_dir, "key.pem")
    port = os.getenv("WEBUI_LISTEN_PORT", "8000")
    use_ssl = os.getenv("WEBUI_SSL", "false").lower() == "true"
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "webui.app:app",
        "--host",
        "0.0.0.0",
        "--port",
        port,
        "--log-level",
        "info",
    ]
    if use_ssl:
        if not (os.path.exists(cert_path) and os.path.exists(key_path)):
            generate_self_signed_cert(cert_path, key_path)
        if os.path.exists(cert_path) and os.path.exists(key_path):
            command.extend(["--ssl-keyfile", key_path, "--ssl-certfile", cert_path])
    process = subprocess.Popen(command, cwd=os.path.join(webui_dir, ".."))
    try:
        while process.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    finally:
        sys.stdout.flush()
        os._exit(0)
