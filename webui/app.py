import os
import sys
import datetime
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn
import boto3
from botocore.client import Config

# Add src directory to path to import Coordinator
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
from agents.coordinator import Coordinator

# S3/MinIO Configuration
MINIO_ENDPOINT = "http://localhost:9000"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin"
MINIO_BUCKET = "lora-data"
FEEDBACK_S3_PREFIX = "feedback"

def get_s3_client():
    """Get configured S3 client for MinIO"""
    return boto3.client('s3',
                        endpoint_url=MINIO_ENDPOINT,
                        aws_access_key_id=MINIO_ACCESS_KEY,
                        aws_secret_access_key=MINIO_SECRET_KEY,
                        config=Config(signature_version='s3v4'),
                        region_name='us-east-1')

def upload_feedback_to_s3(filepath: str):
    """Upload a feedback file to S3"""
    try:
        s3 = get_s3_client()
        filename = os.path.basename(filepath)
        s3_key = f"{FEEDBACK_S3_PREFIX}/{filename}"
        
        s3.upload_file(filepath, MINIO_BUCKET, s3_key)
        print(f"[WebUI] Uploaded feedback to s3://{MINIO_BUCKET}/{s3_key}")
        return True
    except Exception as e:
        print(f"[WebUI] Failed to upload feedback to S3: {e}")
        return False


from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown
    print("\n[WebUI] Shutting down and releasing resources...")
    try:
        coordinator.clear_agents()
    except Exception as e:
        print(f"Error during cleanup: {e}")
    finally:
        print("[WebUI] Forcefully terminating to prevent ROCm hang...")
        sys.stdout.flush()
        os._exit(0)

app = FastAPI(title="DataAlchemy WebUI", lifespan=lifespan)

# Initialize Coordinator
# Note: We use 'python' mode by default for the WebUI
coordinator = Coordinator(mode="python")

class ChatRequest(BaseModel):
    query: str

class ChatResponse(BaseModel):
    answer: str
    feedback_id: str

class FeedbackUpdateRequest(BaseModel):
    feedback_id: str
    feedback: str # "good" or "bad"

@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    if not request.query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    
    try:
        # Use Coordinator to get fused response
        answer = coordinator.chat(request.query)
        # Save initial feedback as "good"
        feedback_id = coordinator.save_feedback(request.query, answer, "good")
        return ChatResponse(answer=answer, feedback_id=feedback_id)
    except Exception as e:
        print(f"Error during chat: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/feedback")
async def update_feedback(request: FeedbackUpdateRequest):
    if request.feedback not in ["good", "bad"]:
        raise HTTPException(status_code=400, detail="Invalid feedback value")
    
    try:
        from config import FEEDBACK_DATA_DIR
        import json
        filepath = os.path.join(FEEDBACK_DATA_DIR, request.feedback_id)
        
        if not os.path.exists(filepath):
            raise HTTPException(status_code=404, detail="Feedback record not found")
            
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        data["feedback"] = request.feedback
        data["updated_at"] = datetime.datetime.now().isoformat()
        
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            
        print(f"[WebUI] Feedback updated for {request.feedback_id} to {request.feedback}")
        
        # Upload to S3
        upload_feedback_to_s3(filepath)
        
        return {"status": "success"}
    except Exception as e:
        print(f"Error updating feedback: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Mount static files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

import subprocess
import time

if __name__ == "__main__":
    webui_dir = os.path.dirname(os.path.abspath(__file__))
    cert_path = os.path.join(webui_dir, "cert.pem")
    key_path = os.path.join(webui_dir, "key.pem")
    
    # Construct uvicorn command
    cmd = [
        sys.executable, "-m", "uvicorn", 
        "webui.app:app",
        "--host", "0.0.0.0",
        "--port", "8443",
        "--log-level", "info"
    ]
    
    if os.path.exists(cert_path) and os.path.exists(key_path):
        print(f"[WebUI] Starting HTTPS server on https://localhost:8443")
        cmd.extend(["--ssl-keyfile", key_path, "--ssl-certfile", cert_path])
    else:
        print(f"[WebUI] Cert files not found. Falling back to HTTP on http://localhost:8000")
        # Fallback to port 8000 if no certs
        cmd[cmd.index("8443")] = "8000"
        
    # Start server as a subprocess
    # This isolates the ROCm/PyTorch process from the launcher
    print("[WebUI] Launching server process...")
    process = subprocess.Popen(cmd, cwd=os.path.join(webui_dir, ".."))
    
    print(f"[WebUI] Server PID: {process.pid}")
    print("[WebUI] Press Ctrl+C to stop.")
    
    try:
        while True:
            time.sleep(1)
            if process.poll() is not None:
                print(f"[WebUI] Server process exited unexpectedly with code {process.returncode}")
                break
    except KeyboardInterrupt:
        print("\n[WebUI] Ctrl+C detected. Terminating server process...")
        process.terminate()
        try:
            process.wait(timeout=3)
            print("[WebUI] Server terminated gracefully.")
        except subprocess.TimeoutExpired:
            print("[WebUI] Server did not exit. Killing...")
            process.kill()
            print("[WebUI] Server killed.")
    finally:
        sys.stdout.flush()
        os._exit(0)
