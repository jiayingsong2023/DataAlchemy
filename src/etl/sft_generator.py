import json
import os
import concurrent.futures
from openai import OpenAI
from config import LLM_CONFIG, SFT_OUTPUT_PATH
from etl.prompts import get_qa_prompt

class SFTGenerator:
    def __init__(self):
        self.client = OpenAI(
            api_key=LLM_CONFIG["api_key"],
            base_url=LLM_CONFIG["base_url"]
        )
        self.model = LLM_CONFIG["model"]

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
                temperature=LLM_CONFIG["temperature"],
                max_tokens=LLM_CONFIG["max_tokens"]
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
                # The LLM returns text in the format:
                # ### Instruction: ...
                # ### Response: ...
                # We save it as a single 'text' field to match train.py expectations
                f.write(json.dumps({"text": res.strip()}) + "\n")
        
        print(f"SFT data generation complete. Saved to: {SFT_OUTPUT_PATH}")

