import concurrent.futures
import json
import os
import time
from typing import Any, Callable

from openai import OpenAI

from config import EXECUTION_MODE, S3_BUCKET, SFT_OUTPUT_PATH, SFT_S3_PATH, get_model_config
from etl.sanitizers import sanitize_for_cloud
from synthesis.prompts import get_multi_turn_prompt, get_qa_prompt
from utils.cloud_audit import observable_model_call, record_cloud_call
from utils.proxy import get_openai_client_kwargs
from utils.s3_utils import S3Utils


class SFTGenerator:
    def __init__(self, output_format="alpaca", mode="single"):
        if EXECUTION_MODE != "cloud":
            raise RuntimeError("SFT synthesis requires EXECUTION_MODE=cloud")
        model_a = get_model_config("model_a")
        self.model = model_a.get("model_id", "deepseek-chat")
        self.base_url = model_a.get("base_url", "https://api.deepseek.com")
        self.api_key = model_a.get("api_key")
        self.output_format = output_format
        self.mode = mode  # "single" or "multi"
        self.s3 = S3Utils()

        print(
            f"[SFTGenerator] Initializing with model={self.model}, format={self.output_format}, mode={self.mode}"
        )

        # Get proxy-aware client kwargs
        client_kwargs = get_openai_client_kwargs()
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, **client_kwargs)
        self.temperature = model_a.get("temperature", 0.7)
        self.max_tokens = model_a.get("max_tokens", 1024)

    def generate_sft_item(
        self,
        context,
        insights=None,
        trace_recorder: Callable[[dict[str, Any]], None] | None = None,
    ):
        """Call LLM to generate QA pairs or Multi-turn dialogue from context."""
        if not context or len(context.strip()) < 50:
            return None

        try:
            if self.mode == "multi":
                prompt = get_multi_turn_prompt(context)
            else:
                prompt = get_qa_prompt(context, insights)

            cloud_prompt = sanitize_for_cloud(prompt)
            record_cloud_call("sft_generator.generate", self.model, ["context"])

            messages = [
                {
                    "role": "system",
                    "content": "You are a helpful assistant that generates expert training data with numerical awareness.",
                },
                {"role": "user", "content": cloud_prompt},
            ]
            generation_config = {
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            started = time.perf_counter()
            response = self.client.chat.completions.create(
                # Input has already passed the cloud PII gate below.
                model=self.model,
                messages=messages,
                **generation_config,
            )
            content = response.choices[0].message.content
            if trace_recorder:
                trace_recorder(
                    observable_model_call(
                        component="sft_generator.generate",
                        model=self.model,
                        messages=messages,
                        response=content,
                        generation_config=generation_config,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        status="succeeded",
                        revision_or_digest=getattr(response, "model", None),
                        usage=response.usage.model_dump() if response.usage else None,
                        provider_request_id=getattr(response, "id", None),
                    )
                )
            return content
        except Exception as e:
            if trace_recorder and "messages" in locals():
                trace_recorder(
                    observable_model_call(
                        component="sft_generator.generate",
                        model=self.model,
                        messages=messages,
                        response=None,
                        generation_config=generation_config,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        status="failed",
                    )
                )
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
                insights_summary = idf.head(5).to_init_repr()  # Quick string representation
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
                return False
            if os.path.isdir(input_path):
                for filename in os.listdir(input_path):
                    if filename.startswith("part-") and filename.endswith(".json"):
                        self._read_jsonl_file(os.path.join(input_path, filename), contexts)
            else:
                self._read_jsonl_file(input_path, contexts)

        if not contexts:
            print(f"No valid data found in: {input_path}")
            return False

        if max_samples:
            contexts = contexts[:max_samples]

        # 2. Run generation logic for all gathered contexts
        print(f"Generating SFT data for {len(contexts)} chunks...")
        return self._generate_and_save(contexts, insights_summary)

    def _generate_and_save(self, contexts, insights=None):
        """The core LLM generation and S3 saving logic."""
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_context = {
                executor.submit(self.generate_sft_item, ctx, insights): ctx for ctx in contexts
            }
            for future in concurrent.futures.as_completed(future_to_context):
                try:
                    res = future.result()
                    if res:
                        if self.mode == "multi":
                            turns = self._parse_multi_turn(res)
                            if turns:
                                results.append({"conversations": turns})
                        else:
                            structured_pairs = self._parse_llm_response(res)
                            formatted_pairs = self._reformat_for_sft(structured_pairs)
                            results.extend(formatted_pairs)
                except Exception as e:
                    print(f"Generation worker failed: {e}")

        if results:
            # Quality Filtering (Phase 2.3)
            results = [r for r in results if self._is_high_quality(r)]

            # 1. Prepare JSONL content
            jsonl_content = (
                "\n".join([json.dumps(res, ensure_ascii=False) for res in results]) + "\n"
            )

            # 2. Upload to S3 (Primary)
            s3_key = SFT_S3_PATH.replace(f"s3://{S3_BUCKET}/", "")
            if not self.s3.put_object(
                s3_key, jsonl_content.encode("utf-8"), content_type="application/x-jsonlines"
            ):
                print("[!] Failed to upload SFT data to S3.")
                return False
            print(f"SFT data uploaded to S3: {SFT_S3_PATH}")

            # 3. Save to Local (Fallback/Debug)
            os.makedirs(os.path.dirname(SFT_OUTPUT_PATH), exist_ok=True)
            with open(SFT_OUTPUT_PATH, "w", encoding="utf-8") as f:
                f.write(jsonl_content)

            # 4. Generate dataset_info.json for LLaMA-Factory
            self._update_dataset_info()

            print(
                f"SFT data generation complete. Saved {len(results)} pairs in {self.output_format} format."
            )
            return True
        else:
            print("No SFT pairs were generated.")
            return False

    def _parse_llm_response(self, response_text):
        """Parse '### Instruction:' and '### Response:' format into structured list."""
        qa_pairs = []
        import re

        pattern = r"### Instruction:(.*?)\n### Response:(.*?)(?=\n### Instruction:|$)"
        matches = re.finditer(pattern, response_text, re.DOTALL)
        for match in matches:
            instruction = match.group(1).strip()
            response = match.group(2).strip()
            if instruction and response:
                qa_pairs.append({"instruction": instruction, "output": response})
        return qa_pairs

    def _parse_multi_turn(self, response_text):
        """Parse '### User:' and '### Assistant:' format into ShareGPT turns."""
        turns = []
        import re

        pattern = r"### User:(.*?)\n### Assistant:(.*?)(?=\n### User:|$)"
        matches = re.finditer(pattern, response_text, re.DOTALL)
        for match in matches:
            u = match.group(1).strip()
            a = match.group(2).strip()
            if u and a:
                turns.append({"from": "human", "value": u})
                turns.append({"from": "gpt", "value": a})
        return turns

    def _reformat_for_sft(self, qa_pairs):
        """Reformat structured QA pairs into the target SFT format."""
        formatted = []
        for pair in qa_pairs:
            if self.output_format == "alpaca":
                formatted.append(
                    {"instruction": pair["instruction"], "input": "", "output": pair["output"]}
                )
            elif self.output_format == "sharegpt":
                formatted.append(
                    {
                        "conversations": [
                            {"from": "human", "value": pair["instruction"]},
                            {"from": "gpt", "value": pair["output"]},
                        ]
                    }
                )
            else:
                formatted.append(pair)
        return formatted

    def _is_high_quality(self, record):
        """Basic quality check for generated record."""
        # Check for minimum length of response
        if self.output_format == "alpaca":
            res = record.get("output", "")
        elif self.output_format == "sharegpt":
            res = record.get("conversations", [{}])[-1].get("value", "")
        else:
            res = str(record)

        if len(res) < 30:
            return False
        if "I don't know" in res or "抱歉" in res:
            return False
        return True

    def _update_dataset_info(self):
        """Create/Update dataset_info.json for LLaMA-Factory integration."""
        info_path = os.path.join(os.path.dirname(SFT_OUTPUT_PATH), "dataset_info.json")
        dataset_name = "data_alchemy_sft"

        entry = {
            "file_name": os.path.basename(SFT_OUTPUT_PATH),
            "columns": {"prompt": "instruction", "query": "input", "response": "output"}
            if self.output_format == "alpaca"
            else {"messages": "conversations"},
        }

        info = {}
        if os.path.exists(info_path):
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
            except:
                pass

        info[dataset_name] = entry
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=4, ensure_ascii=False)
        print(f"[*] Updated dataset_info.json at {info_path}")

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
                if obj["Key"].endswith(".json") and "part-" in obj["Key"]:
                    body = s3_util.get_object_body(obj["Key"])
                    if body:
                        for line in body.decode("utf-8").splitlines():
                            if line.strip():
                                try:
                                    record = json.loads(line)
                                    if record.get("text"):
                                        contexts.append(record["text"])
                                except:
                                    continue

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
