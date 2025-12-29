import json
import os
import concurrent.futures
from openai import OpenAI
from config import get_model_config, SFT_OUTPUT_PATH
from synthesis.prompts import get_qa_prompt

class SFTGenerator:
    def __init__(self):
        model_a = get_model_config("model_a")
        self.model = model_a.get("model_id", "deepseek-chat")
        self.base_url = model_a.get("base_url", "https://api.deepseek.com")
        self.api_key = model_a.get("api_key")
        
        print(f"[SFTGenerator] Initializing with model={self.model}, base_url={self.base_url}")
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )
        self.temperature = model_a.get("temperature", 0.7)
        self.max_tokens = model_a.get("max_tokens", 1024)

    def generate_qa_pair(self, context):
        """Call LLM to generate QA pairs from a single context chunk."""
        if not context or len(context.strip()) < 50:
            return None
            
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that generates training data."},
                    {"role": "user", "content": get_qa_prompt(context)}
                ],
                temperature=self.temperature,
                max_tokens=self.max_tokens
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"Error calling LLM: {e}")
            return None

    def process_corpus(self, input_jsonl_path, max_samples=None):
        """Read Spark output and generate SFT data using multiple threads."""
        if not os.path.exists(input_jsonl_path):
            print(f"Input file not found: {input_jsonl_path}")
            return

        contexts = []
        with open(input_jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    contexts.append(data.get("text", ""))
                except:
                    continue
        
        if max_samples:
            contexts = contexts[:max_samples]

        print(f"Generating SFT data for {len(contexts)} chunks...")
        
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_context = {executor.submit(self.generate_qa_pair, ctx): ctx for ctx in contexts}
            for future in concurrent.futures.as_completed(future_to_context):
                res = future.result()
                if res:
                    results.append(res)
        
        # Save to SFT output path
        os.makedirs(os.path.dirname(SFT_OUTPUT_PATH), exist_ok=True)
        with open(SFT_OUTPUT_PATH, "w", encoding="utf-8") as f:
            for res in results:
                f.write(json.dumps({"text": res.strip()}) + "\n")
        
        print(f"SFT data generation complete. Saved to: {SFT_OUTPUT_PATH}")
