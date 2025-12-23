"""
Agent A: The Cleaner (Data Alchemy).
Wraps the Spark/Python ETL engines.
"""
from spark_etl.main import get_engine
from config import FINAL_OUTPUT_PATH, RAG_CHUNKS_PATH
import os
import json

class AgentA:
    def __init__(self, mode="python"):
        self.engine = get_engine(mode)

    def clean_and_split(self):
        """Perform data cleaning and semantic chunking."""
        print("[Agent A] Starting data cleaning pipeline...")
        results = self.engine.process_all()
        
        sft_data = results.get("sft", [])
        rag_data = results.get("rag", [])
        
        # Save SFT data
        if sft_data:
            os.makedirs(os.path.dirname(FINAL_OUTPUT_PATH), exist_ok=True)
            with open(FINAL_OUTPUT_PATH, 'w', encoding='utf-8') as f:
                for item in sft_data:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            print(f"[Agent A] SFT data saved to {FINAL_OUTPUT_PATH}")
            
        # Save RAG chunks
        if rag_data:
            os.makedirs(os.path.dirname(RAG_CHUNKS_PATH), exist_ok=True)
            with open(RAG_CHUNKS_PATH, 'w', encoding='utf-8') as f:
                for item in rag_data:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            print(f"[Agent A] RAG chunks saved to {RAG_CHUNKS_PATH}")
            
        return results

