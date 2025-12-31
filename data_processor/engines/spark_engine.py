import os
import json
from pyspark.sql import SparkSession

# Import specialized cleaners
from cleaners.git_pr import process_git_pr
from cleaners.jira import process_jira
from cleaners.document import process_documents
from cleaners.confluence import process_confluence
from cleaners.feedback import process_feedback

class SparkEngine:
    def __init__(self, master=None, app_name="K8sSparkWash"):
        # Use master from env if provided, else default to local[*]
        self.master = master or os.environ.get("SPARK_MASTER", "local[*]")
        
        builder = SparkSession.builder.appName(app_name).master(self.master)
        
        # Kubernetes specific configurations
        if self.master.startswith("k8s://"):
            # The container image must be available to the K8s cluster
            image = os.environ.get("SPARK_IMAGE", "data-processor:latest")
            print(f"[*] Configuring Spark on K8s with 2 specialized executor pods for scaling...")
            builder = builder \
                .config("spark.kubernetes.container.image", image) \
                .config("spark.kubernetes.authenticate.driver.serviceAccountName", "spark") \
                .config("spark.executor.instances", "2") \
                .config("spark.kubernetes.namespace", "default")
        
        self.spark = builder \
            .config("spark.ui.showConsoleProgress", "false") \
            .getOrCreate()
            
        self.spark.sparkContext.setLogLevel("WARN")

    def process_all(self, input_path, output_path, chunk_size=500, overlap=50):
        dfs = []
        print(f"[*] Processing data from: {input_path}")
        
        # Mapping sources to their processor functions
        # Note: input_path is usually data/raw
        source_configs = [
            ("git_pr", os.path.join(input_path, "git_pr"), process_git_pr),
            ("jira", os.path.join(input_path, "jira"), process_jira),
            ("documents", os.path.join(input_path, "documents"), process_documents),
            ("confluence", os.path.join(input_path, "confluence"), process_confluence),
            ("feedback", input_path, process_feedback) # feedback logic handles its own path resolution
        ]

        for source_name, source_path, processor in source_configs:
            print(f"  - Cleaning {source_name}...")
            df = processor(self.spark, source_path)
            if df:
                dfs.append(df)

        if not dfs:
            print("[!] No data found.")
            return

        final_df = dfs[0]
        for next_df in dfs[1:]:
            final_df = final_df.union(next_df)
        
        final_df = final_df.cache()

        # 1. Save Rough-Cleaned Corpus
        print("[*] Collecting rough-cleaned corpus...")
        results = [{"text": row.text} for row in final_df.collect()]
        
        washed_file = os.path.join(output_path, "cleaned_corpus.jsonl")
        os.makedirs(output_path, exist_ok=True)
        with open(washed_file, 'w', encoding='utf-8') as f:
            for item in results:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"[SUCCESS] Saved {len(results)} rough-cleaned records to {washed_file}")

        # 2. Generate RAG Chunks
        print("[*] Generating RAG chunks...")
        rag_chunks = []
        for row in results:
            text = row["text"]
            chunks = self._chunk_text(text, chunk_size, overlap)
            for i, chunk in enumerate(chunks):
                rag_chunks.append({
                    "text": chunk,
                    "metadata": {"chunk_id": i, "source": "data_processor"}
                })
        
        rag_file = os.path.join(output_path, "rag_chunks.jsonl")
        with open(rag_file, 'w', encoding='utf-8') as f:
            for item in rag_chunks:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"[SUCCESS] Saved {len(rag_chunks)} RAG chunks to {rag_file}")

    def _chunk_text(self, text, chunk_size, overlap):
        if not text: return []
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunks.append(text[start:end])
            if end >= len(text): break
            start += (chunk_size - overlap)
        return chunks

    def stop(self):
        self.spark.stop()
