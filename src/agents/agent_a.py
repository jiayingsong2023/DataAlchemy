"""
Agent A: The Cleaner (Data Alchemy).
Wraps the Spark/Python ETL engines. Supports cross-environment Spark execution via WSL.
"""
import os
import json
import subprocess
import sys
from config import WASHED_DATA_PATH, RAG_CHUNKS_PATH

class AgentA:
    def __init__(self, mode="spark"):
        self.mode = mode
        self.engine = None

    def clean_and_split(self):
        """Perform data cleaning and semantic chunking using Spark on Kubernetes."""
        print(f"[Agent A] Starting data cleaning pipeline (Mode: {self.mode})...")
        
        # 1. K8s Mode -> Trigger Job in Kubernetes
        print("[Agent A] Attempting to trigger Spark cleaning in K8s...", flush=True)
        try:
            # Find project root relative to this file: src/agents/agent_a.py -> project_root
            current_file_dir = os.path.dirname(os.path.abspath(__file__))
            base_dir = os.path.dirname(os.path.dirname(current_file_dir))
            
            rbac_path = os.path.normpath(os.path.join(base_dir, "k8s", "spark-rbac.yaml"))
            job_path = os.path.normpath(os.path.join(base_dir, "k8s", "spark-job.yaml"))

            # 1. Ensure RBAC is applied
            print(f"[Agent A] Applying RBAC: {rbac_path.replace('\\', '/')}", flush=True)
            if not os.path.exists(rbac_path):
                print(f"[ERROR] RBAC file missing at {rbac_path}", flush=True)
                raise FileNotFoundError(f"Missing K8s config: {rbac_path}")
            
            subprocess.run(f'kubectl apply -f "{rbac_path}"', shell=True, check=True)
            
            # 2. Cleanup previous job if exists
            subprocess.run("kubectl delete job spark-data-cleaner --ignore-not-found=true", shell=True, check=True)
            
            # 3. Apply the Spark Job
            print(f"[Agent A] Submitting K8s Job: {job_path.replace('\\', '/')}", flush=True)
            subprocess.run(f'kubectl apply -f "{job_path}"', shell=True, check=True)
            
            # 4. Wait for Job completion
            print("[Agent A] Waiting for Spark Job to complete (timeout 300s)...", flush=True)
            subprocess.run("kubectl wait --for=condition=complete job/spark-data-cleaner --timeout=300s", shell=True, check=True)
            
            print("[Agent A] K8s Spark task completed successfully.", flush=True)
            return {"status": "success", "engine": "spark_on_k8s"}
            
        except Exception as e:
            print(f"[Agent A] K8s execution failed: {e}.", flush=True)
            sys.exit(1)
