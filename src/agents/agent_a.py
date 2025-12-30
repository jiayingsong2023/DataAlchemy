"""
Agent A: The Cleaner (Data Alchemy).
Wraps the Spark/Python ETL engines. Supports cross-environment Spark execution via WSL.
"""
import os
import json
import subprocess
import sys
from config import WASHED_DATA_PATH, RAG_CHUNKS_PATH, is_wsl, to_wsl_path

class AgentA:
    def __init__(self, mode="python"):
        self.mode = mode
        self.engine = None

    def _get_local_engine(self):
        """Lazy load local engine to avoid unnecessary dependencies."""
        # Add data_processor to path to allow importing its engines
        # Root is 3 levels up from src/agents/agent_a.py
        root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        processor_path = os.path.join(root_dir, "data_processor")
        if processor_path not in sys.path:
            sys.path.append(processor_path)
            
        from engines.python_engine import PythonEngine
        return PythonEngine()

    def clean_and_split(self):
        """Perform data cleaning and semantic chunking."""
        print(f"[Agent A] Starting data cleaning pipeline (Mode: {self.mode})...")
        
        # 1. K8s Mode -> Trigger Job in Kubernetes
        if self.mode == "spark" and not is_wsl():
            print("[Agent A] Attempting to trigger Spark cleaning in K8s...", flush=True)
            try:
                # Find project root relative to this file: src/agents/agent_a.py -> project_root
                # This ensures we find /k8s and /data_processor correctly even when run from root
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
                print("[Agent A] Falling back to WSL...", flush=True)
                pass

        # 2. Spark Mode in Windows -> Trigger WSL Standalone Project
        if self.mode == "spark" and not is_wsl():
            print("[Agent A] Triggering Spark cleaning in WSL (Standalone Project)...")
            try:
                # Use project root for stable WSL paths
                current_file_dir = os.path.dirname(os.path.abspath(__file__))
                win_root = os.path.dirname(os.path.dirname(current_file_dir))
                wsl_root = to_wsl_path(win_root)
                
                raw_data_wsl = f"{wsl_root}/data/raw"
                output_data_wsl = f"{wsl_root}/data"
                processor_dir_wsl = f"{wsl_root}/data_processor"
                
                inner_cmd = f"cd {processor_dir_wsl} && uv run python main.py --input {raw_data_wsl} --output {output_data_wsl}"
                cmd = ["wsl", "bash", "-l", "-c", inner_cmd]
                
                print(f"[Agent A] Executing in WSL: {inner_cmd}")
                
                # Run and stream output
                result = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr)
                
                if result.returncode != 0:
                    print(f"[ERROR] WSL Spark task failed with return code {result.returncode}")
                    return {"status": "error", "message": "WSL task failed"}
                
                print("[Agent A] WSL Spark task completed successfully.")
                return {"status": "success", "engine": "spark_via_wsl"}
                
            except Exception as e:
                print(f"[ERROR] Failed to trigger WSL: {e}")
                return {"status": "error", "message": str(e)}

        # 2. Python Mode (or Spark if already in WSL)
        if not self.engine:
            self.engine = self._get_local_engine()
            
        results = self.engine.process_all()
        
        # Save results locally if they were returned in memory (Python engine does this)
        sft_data = results.get("sft", [])
        rag_data = results.get("rag", [])
        
        if sft_data:
            os.makedirs(os.path.dirname(WASHED_DATA_PATH), exist_ok=True)
            with open(WASHED_DATA_PATH, 'w', encoding='utf-8') as f:
                for item in sft_data:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            print(f"[Agent A] Rough-cleaned corpus saved to {WASHED_DATA_PATH}")
            
        if rag_data:
            os.makedirs(os.path.dirname(RAG_CHUNKS_PATH), exist_ok=True)
            with open(RAG_CHUNKS_PATH, 'w', encoding='utf-8') as f:
                for item in rag_data:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            print(f"[Agent A] RAG chunks saved to {RAG_CHUNKS_PATH}")
            
        return results
