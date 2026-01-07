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

    def process_corpus(self, input_path, max_samples=None):
        """Read corpus (Local or S3) and generate SFT data."""
        contexts = []
        
        # 1. Load contexts from either S3 or Local
        if input_path.startswith("s3a://") or input_path.startswith("s3://"):
            contexts = self._read_from_s3(input_path)
        else:
            if not os.path.exists(input_path):
                print(f"Input path not found: {input_path}")
                return
            if os.path.isdir(input_path):
                for filename in os.listdir(input_path):
                    if filename.startswith("part-") and filename.endswith(".json"):
                        self._read_jsonl_file(os.path.join(input_path, filename), contexts)
            else:
                self._read_jsonl_file(input_path, contexts)
        
        if not contexts:
            print(f"No valid data found in: {input_path}")
            return

        if max_samples:
            contexts = contexts[:max_samples]

        # 2. Run generation logic for all gathered contexts
        print(f"Generating SFT data for {len(contexts)} chunks...")
        self._generate_and_save(contexts)

    def _generate_and_save(self, contexts):
        """The core LLM generation and local saving logic."""
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_context = {executor.submit(self.generate_qa_pair, ctx): ctx for ctx in contexts}
            for future in concurrent.futures.as_completed(future_to_context):
                try:
                    res = future.result()
                    if res:
                        results.append(res)
                except Exception as e:
                    print(f"Generation worker failed: {e}")
        
        if results:
            os.makedirs(os.path.dirname(SFT_OUTPUT_PATH), exist_ok=True)
            # Force update timestamp by reopening in 'w' mode
            with open(SFT_OUTPUT_PATH, "w", encoding="utf-8") as f:
                for res in results:
                    f.write(json.dumps({"text": res.strip()}, ensure_ascii=False) + "\n")
            
            # Explicitly touch the file if needed (optional, 'w' already updates)
            import time
            os.utime(SFT_OUTPUT_PATH, None)
            
            print(f"SFT data generation complete. Saved {len(results)} pairs to: {SFT_OUTPUT_PATH}")
        else:
            print("No SFT pairs were generated.")

    def _read_from_s3(self, s3_path):
        """Download and parse JSONL files from MinIO."""
        print(f"[*] Reading coarse-cleaned data from S3: {s3_path}")
        try:
            import boto3
            from botocore.client import Config
            from config import S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY
            
            # Parse bucket and prefix
            path_parts = s3_path.replace("s3a://", "").replace("s3://", "").split("/")
            bucket = path_parts[0]
            prefix = "/".join(path_parts[1:])
            
            s3 = boto3.client('s3', 
                              endpoint_url=S3_ENDPOINT, 
                              aws_access_key_id=S3_ACCESS_KEY, 
                              aws_secret_access_key=S3_SECRET_KEY,
                              config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}),
                              region_name='us-east-1')
            
            # Spark outputs directory with partitioned files
            response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
            contexts = []
            for obj in response.get('Contents', []):
                # Check for files in directory if no direct match
                if obj['Key'].endswith(".json") and "part-" in obj['Key']:
                    data = s3.get_object(Bucket=bucket, Key=obj['Key'])
                    for line in data['Body'].read().decode('utf-8').splitlines():
                        if line.strip():
                            try:
                                record = json.loads(line)
                                if record.get("text"):
                                    contexts.append(record["text"])
                            except: continue
            
            # Try appending a slash if no contents found (common Spark behavior)
            if not contexts:
                response = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/")
                for obj in response.get('Contents', []):
                    if obj['Key'].endswith(".json") and "part-" in obj['Key']:
                        data = s3.get_object(Bucket=bucket, Key=obj['Key'])
                        for line in data['Body'].read().decode('utf-8').splitlines():
                            if line.strip():
                                try:
                                    record = json.loads(line)
                                    if record.get("text"):
                                        contexts.append(record["text"])
                                except: continue
            
            return contexts
        except Exception as e:
            print(f"[!] S3 Read failed: {e}")
            return []
    
    def _read_jsonl_file(self, file_path, contexts):
        """Helper to read a single local JSONL file."""
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    if data.get("text"):
                        contexts.append(data.get("text"))
                except:
                    continue
