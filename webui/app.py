import os
import sys
import datetime
from datetime import timedelta
import asyncio
import json
import logging

# Suppress noisy Windows Proactor errors and unwanted 404s
class LogFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        # Suppress Windows Connection Reset (10054)
        if "10054" in msg: return False
        # Suppress the old API status 404 while browser caches clear
        if "/api/status" in msg and "404" in msg: return False
        return True

logging.getLogger("uvicorn.error").addFilter(LogFilter())
logging.getLogger("uvicorn.access").addFilter(LogFilter())

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn
import boto3
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi import Response
from botocore.client import Config
from typing import List, Optional
from utils.logger import logger

# Add src directory to path to import Coordinator
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
from agents.coordinator import Coordinator
from config import (
    S3_ENDPOINT as MINIO_ENDPOINT,
    S3_ACCESS_KEY as MINIO_ACCESS_KEY,
    S3_SECRET_KEY as MINIO_SECRET_KEY,
    S3_BUCKET as MINIO_BUCKET
)
from utils.auth import (
    create_access_token, verify_password, get_current_user, 
    decode_token, ACCESS_TOKEN_EXPIRE_MINUTES
)
from utils.user_db import get_user
from fastapi.security import OAuth2PasswordRequestForm

# S3/MinIO Configuration (Now imported from config.py)
FEEDBACK_S3_PREFIX = "feedback"

def get_s3_client():
    """Get configured S3 client for MinIO"""
    return boto3.client('s3',
                        endpoint_url=MINIO_ENDPOINT,
                        aws_access_key_id=MINIO_ACCESS_KEY,
                        aws_secret_access_key=MINIO_SECRET_KEY,
                        config=Config(
                            signature_version='s3v4',
                            s3={'addressing_style': 'path'} # 强制使用路径风格
                        ),
                        region_name='us-east-1')

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting background knowledge sync...")
    coordinator.start_knowledge_sync()
    yield
    # Shutdown
    logger.info("Shutting down and releasing resources...")
    try:
        coordinator.clear_agents()
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
    finally:
        logger.info("Forcefully terminating to prevent ROCm hang...")
        sys.stdout.flush()
        os._exit(0)

app = FastAPI(title="DataAlchemy WebUI", lifespan=lifespan)

@app.get("/metrics")
async def metrics():
    logger.info("Metrics endpoint hit")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

# Initialize Coordinator
# Note: We use 'python' mode by default for the WebUI
coordinator = Coordinator(mode="python")

from fastapi import WebSocket, WebSocketDisconnect

@app.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    # Token validation for WebSockets
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
        
    username = decode_token(token)
    if not username:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    logger.info(f"WebSocket connection accepted for user: {username}")
    await websocket.accept()
    
    try:
        while True:
            data = await websocket.receive_text()
            request_data = json.loads(data)
            query = request_data.get("query")
            
            if not query:
                await websocket.send_json({"error": "Query cannot be empty"})
                continue
            
            logger.info(f"WebSocket query from {username}: {query}")
            
            session_id = request_data.get("session_id")
            
            await websocket.send_json({"type": "status", "content": "Retrieving knowledge..."})
            
            # 1. Agent C: Retrieve Knowledge
            self_coord = coordinator
            self_coord._lazy_load_agents(need_c=True)
            loop = asyncio.get_event_loop()
            context = await loop.run_in_executor(None, self_coord.agent_c.query, query)
            
            await websocket.send_json({"type": "status", "content": "Consulting LoRA model..."})
            
            # 2. Agent B: Get Model Intuition
            self_coord._lazy_load_agents(need_b=True)
            intuition = await self_coord.agent_b.predict_async(query)
            
            await websocket.send_json({"type": "status", "content": "Fusing response..."})
            
            # 3. Agent D: Final Fusion
            self_coord._lazy_load_agents(need_d=True)
            final_answer = await loop.run_in_executor(
                None, 
                self_coord.agent_d.fuse_and_respond, 
                query, context, intuition
            )
            
            # Determine session
            self_coord.agent_b._ensure_engine()
            if not session_id:
                session_id = await self_coord.agent_b.batch_engine.cache.create_session(username)
            
            # Save to Redis session history
            await self_coord.agent_b.batch_engine.cache.add_message_to_session(session_id, {
                "query": query,
                "answer": final_answer,
                "timestamp": datetime.datetime.now().isoformat()
            })
            
            # Save feedback
            feedback_id = self_coord.save_feedback(query, final_answer, "good")
            
            # Send final answer
            await websocket.send_json({
                "type": "answer", 
                "content": final_answer,
                "feedback_id": feedback_id,
                "session_id": session_id
            })
            
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
        try:
            await websocket.send_json({"error": str(e)})
        except:
            pass

class ChatRequest(BaseModel):
    query: str
    session_id: Optional[str] = None

class SessionCreate(BaseModel):
    title: Optional[str] = "New Chat"

class ChatResponse(BaseModel):
    answer: str
    feedback_id: str
    session_id: str

class FeedbackUpdateRequest(BaseModel):
    feedback_id: str
    feedback: str # "good" or "bad"


class Token(BaseModel):
    access_token: str
    token_type: str

@app.post("/api/auth/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = get_user(form_data.username)
    if not user or not verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"]}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/api/auth/me")
async def read_users_me(current_user: str = Depends(get_current_user)):
    return {"username": current_user}

@app.get("/api/sessions")
async def list_sessions(current_user: str = Depends(get_current_user)):
    coordinator._lazy_load_agents(need_b=True)
    coordinator.agent_b._ensure_engine()
    cache = coordinator.agent_b.batch_engine.cache
    sessions = await cache.list_sessions(current_user)
    logger.info(f"API: Found {len(sessions)} sessions for user {current_user}")
    return {"sessions": sessions}

@app.post("/api/sessions")
async def create_session(request: SessionCreate, current_user: str = Depends(get_current_user)):
    coordinator._lazy_load_agents(need_b=True)
    coordinator.agent_b._ensure_engine()
    session_id = await coordinator.agent_b.batch_engine.cache.create_session(current_user, request.title)
    return {"session_id": session_id}

@app.get("/api/sessions/{session_id}")
async def get_session_history(session_id: str, current_user: str = Depends(get_current_user)):
    coordinator._lazy_load_agents(need_b=True)
    coordinator.agent_b._ensure_engine()
    messages = await coordinator.agent_b.batch_engine.cache.get_session_messages(session_id)
    return {"messages": messages}

@app.get("/api/history")
async def get_history(current_user: str = Depends(get_current_user)):
    # Legacy endpoint
    coordinator._lazy_load_agents(need_b=True)
    coordinator.agent_b._ensure_engine()
    history = await coordinator.agent_b.batch_engine.cache.get_chat_history(current_user)
    return {"history": history}

@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, current_user: str = Depends(get_current_user)):
    if not request.query:
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    
    try:
        # Use Coordinator to get fused response (async)
        answer = await coordinator.chat_async(request.query)
        
        # Determine session
        coordinator._lazy_load_agents(need_b=True)
        coordinator.agent_b._ensure_engine()
        session_id = request.session_id
        if not session_id:
            session_id = await coordinator.agent_b.batch_engine.cache.create_session(current_user)
            
        # Save to Redis session history
        await coordinator.agent_b.batch_engine.cache.add_message_to_session(session_id, {
            "query": request.query,
            "answer": answer,
            "timestamp": datetime.datetime.now().isoformat()
        })
        
        # Save feedback record (file-based)
        feedback_id = coordinator.save_feedback(request.query, answer, "good")
        return ChatResponse(answer=answer, feedback_id=feedback_id, session_id=session_id)
    except Exception as e:
        logger.error(f"Error during chat: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/feedback")
async def update_feedback(request: FeedbackUpdateRequest, current_user: str = Depends(get_current_user)):
    """Update feedback status in S3."""
    if request.feedback not in ["good", "bad"]:
        raise HTTPException(status_code=400, detail="Invalid feedback value")
    
    try:
        s3 = get_s3_client()
        s3_key = f"{FEEDBACK_S3_PREFIX}/{request.feedback_id}"
        
        # 1. Download existing
        response = s3.get_object(Bucket=MINIO_BUCKET, Key=s3_key)
        data = json.loads(response['Body'].read().decode('utf-8'))
        
        # 2. Update
        data["feedback"] = request.feedback
        data["updated_at"] = datetime.datetime.now().isoformat()
        
        # 3. Upload back
        s3.put_object(
            Bucket=MINIO_BUCKET,
            Key=s3_key,
            Body=json.dumps(data, ensure_ascii=False, indent=2),
            ContentType="application/json"
        )
        
        logger.info(f"Feedback updated in S3 for {request.feedback_id} to {request.feedback}")
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error updating feedback in S3: {e}")
        raise HTTPException(status_code=500, detail=f"S3 Update failed: {str(e)}")

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
