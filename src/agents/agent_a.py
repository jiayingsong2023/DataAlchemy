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
        
        # 1. Spark Mode in Windows -> Trigger WSL Standalone Project
        if self.mode == "spark" and not is_wsl():
            print("[Agent A] Triggering Spark cleaning in WSL (Standalone Project)...")
            try:
                win_cwd = os.getcwd()
                wsl_cwd = to_wsl_path(win_cwd)
                
                # Input/Output paths in WSL format
                raw_data_wsl = to_wsl_path(os.path.join(win_cwd, "data", "raw"))
                output_data_wsl = to_wsl_path(os.path.join(win_cwd, "data"))
                
                # Command to run in WSL:
                # 1. cd to the standalone project
                # 2. uv run python main.py ...
                processor_dir_wsl = f"{wsl_cwd}/data_processor"
                
                # We use 'bash -l -c' to ensure PATH is loaded (so 'uv' is found)
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
