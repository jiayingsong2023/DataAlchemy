import concurrent.futures
import json
import os

from openai import OpenAI

from config import S3_BUCKET, SFT_OUTPUT_PATH, SFT_S3_PATH, get_model_config
from synthesis.prompts import get_qa_prompt
from utils.proxy import get_openai_client_kwargs
from utils.s3_utils import S3Utils


class SFTGenerator:
    def __init__(self):
        model_a = get_model_config("model_a")
        self.model = model_a.get("model_id", "deepseek-chat")
        self.base_url = model_a.get("base_url", "https://api.deepseek.com")
        self.api_key = model_a.get("api_key")
        self.s3 = S3Utils()

        print(f"[SFTGenerator] Initializing with model={self.model}, base_url={self.base_url}")

        # Get proxy-aware client kwargs
        client_kwargs = get_openai_client_kwargs()
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            **client_kwargs
        )
        self.temperature = model_a.get("temperature", 0.7)
        self.max_tokens = model_a.get("max_tokens", 1024)

    def generate_qa_pair(self, context, insights=None):
        """Call LLM to generate QA pairs from a single context chunk."""
        if not context or len(context.strip()) < 50:
            return None

        try:
            prompt = get_qa_prompt(context, insights)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that generates expert training data with numerical awareness."},
                    {"role": "user", "content": prompt}
                ],
                temperature=self.temperature,
                max_tokens=self.max_tokens
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"Error calling LLM: {e}")
            return None

    def process_corpus(self, input_path, max_samples=None, insight_path=None):
        """Read corpus (Local or S3) and generate SFT data."""
        contexts = []
        insights_summary = None

        # 0. Load Insights if provided
        if insight_path and os.path.exists(insight_path):
            try:
                import polars as pl
                # Just take a summary or the top 5 rows of insights to avoid context window blowup
                idf = pl.read_parquet(insight_path)
                insights_summary = idf.head(5).to_init_repr() # Quick string representation
                print(f"[SFTGenerator] Loaded numerical insights (Quant) from {insight_path}")
            except Exception as e:
                print(f"[!] Failed to load quant insights: {e}")

        # 1. Load contexts from either S3 or Local
        # ...
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
        self._generate_and_save(contexts, insights_summary)

    def _generate_and_save(self, contexts, insights=None):
        """The core LLM generation and S3 saving logic."""
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_context = {executor.submit(self.generate_qa_pair, ctx, insights): ctx for ctx in contexts}
            for future in concurrent.futures.as_completed(future_to_context):
                try:
                    res = future.result()
                    if res:
                        results.append(res)
                except Exception as e:
                    print(f"Generation worker failed: {e}")

        if results:
            # 1. Prepare JSONL content
            jsonl_content = "\n".join([json.dumps({"text": res.strip()}, ensure_ascii=False) for res in results]) + "\n"

            # 2. Upload to S3 (Primary)
            s3_key = SFT_S3_PATH.replace(f"s3://{S3_BUCKET}/", "")
            if self.s3.put_object(s3_key, jsonl_content.encode('utf-8'), content_type="application/x-jsonlines"):
                print(f"SFT data uploaded to S3: {SFT_S3_PATH}")
            else:
                print("[!] Failed to upload SFT data to S3.")

            # 3. Save to Local (Fallback/Debug)
            os.makedirs(os.path.dirname(SFT_OUTPUT_PATH), exist_ok=True)
            with open(SFT_OUTPUT_PATH, "w", encoding="utf-8") as f:
                f.write(jsonl_content)

            print(f"SFT data generation complete. Saved {len(results)} pairs.")
        else:
            print("No SFT pairs were generated.")

    def _read_from_s3(self, s3_path):
        """Download and parse JSONL files from MinIO."""
        print(f"[*] Reading coarse-cleaned data from S3: {s3_path}")
        try:
            # Parse bucket and prefix
            path_parts = s3_path.replace("s3a://", "").replace("s3://", "").split("/")
            bucket = path_parts[0]
            prefix = "/".join(path_parts[1:])

            s3_util = self.s3 if bucket == self.s3.bucket else S3Utils(bucket=bucket)

            # Spark outputs directory with partitioned files
            objects = s3_util.list_objects(prefix)
            if not objects:
                objects = s3_util.list_objects(f"{prefix}/")

            contexts = []
            for obj in objects:
                # Check for files in directory if no direct match
                if obj['Key'].endswith(".json") and "part-" in obj['Key']:
                    body = s3_util.get_object_body(obj['Key'])
                    if body:
                        for line in body.decode('utf-8').splitlines():
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
