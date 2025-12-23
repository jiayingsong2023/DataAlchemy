import json
from openai import OpenAI
from spark_etl.config import LLM_CONFIG
from typing import List, Dict, Any

class AgentD:
    """Agent D: The Finalist (Fusion & Summarization)."""
    
    def __init__(self):
        self.client = OpenAI(
            api_key=LLM_CONFIG["api_key"],
            base_url=LLM_CONFIG["base_url"]
        )
        self.model = LLM_CONFIG["model"]

    def fuse_and_respond(self, query: str, rag_context: List[Dict[str, Any]], lora_intuition: str) -> str:
        """
        Merge RAG facts and LoRA intuition into a final answer using DeepSeek.
        """
        print(f"[Agent D] Fusing evidence for final response...")
        
        # Format RAG context
        context_str = "\n".join([
            f"- [{d['metadata'].get('source', 'Unknown')}] {d['text']}" 
            for d in rag_context
        ]) if rag_context else "No direct evidence found in knowledge base."
        
        system_prompt = (
            "You are a highly intelligent enterprise AI assistant. Your task is to provide an accurate, "
            "concise, and reliable answer based on two sources of information:\n"
            "1. RAG Context: Hard facts retrieved from documentation.\n"
            "2. Model Intuition: Preliminary understanding from a fine-tuned domain model.\n\n"
            "Combine these sources. If they conflict, prioritize the RAG Context as it contains raw facts. "
            "If the model intuition provides useful reasoning or domain-specific terminology, incorporate it."
        )
        
        user_content = (
            f"User Question: {query}\n\n"
            f"--- RAG EVIDENCE ---\n{context_str}\n\n"
            f"--- MODEL INTUITION ---\n{lora_intuition}\n\n"
            "Final Answer:"
        )
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                temperature=0.3, # Low temperature for factual consistency
                max_tokens=1024
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            return f"[Agent D] Error during final fusion: {e}"

