import os
import sys
import json
import argparse
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, concat_ws, lit, udf
from pyspark.sql.types import StringType
import re
from bs4 import BeautifulSoup
import io

# --- Minimal Config ---
# We use environment variables or CLI args instead of importing from the main project
DEFAULT_INPUT_PATH = "/mnt/c/Users/Administrator/work/LoRA/data/raw"
DEFAULT_OUTPUT_PATH = "/mnt/c/Users/Administrator/work/LoRA/data"

# --- UDFs and Cleaners ---
def clean_html(text):
    if not text: return ""
    try:
        soup = BeautifulSoup(text, "html.parser")
        return soup.get_text(separator=" ").strip()
    except:
        return re.sub(r'<[^>]+>', '', text).strip()

def normalize_whitespace(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', text).strip()

clean_html_udf = udf(clean_html, StringType())
normalize_whitespace_udf = udf(normalize_whitespace, StringType())

# --- Spark Engine Logic ---
class SparkEngine:
    def __init__(self, master="local[*]", app_name="StandaloneSparkWash"):
        self.spark = SparkSession.builder \
            .appName(app_name) \
            .master(master) \
            .config("spark.ui.showConsoleProgress", "false") \
            .getOrCreate()
        self.spark.sparkContext.setLogLevel("WARN")

    def _read_json_files(self, path):
        data = []
        if not os.path.exists(path): return data
        for f in os.listdir(path):
            if f.endswith('.json'):
                with open(os.path.join(path, f), 'r', encoding='utf-8') as file:
                    for line in file:
                        if line.strip(): data.append(json.loads(line))
        return data

    def process_git_pr(self, path):
        data = self._read_json_files(os.path.join(path, "git_pr"))
        if not data: return None
        df = self.spark.createDataFrame(data)
        text_parts = [lit("### Git PR Source")]
        if "title" in df.columns: text_parts.append(concat_ws(": ", lit("Title"), col("title")))
        if "description" in df.columns: text_parts.append(concat_ws(": ", lit("Description"), clean_html_udf(col("description"))))
        return df.select(normalize_whitespace_udf(concat_ws("\n\n", *text_parts)).alias("text"))

    def process_jira(self, path):
        data = self._read_json_files(os.path.join(path, "jira"))
        if not data: return None
        df = self.spark.createDataFrame(data)
        text_parts = [lit("### Jira Issue")]
        if "summary" in df.columns: text_parts.append(concat_ws(": ", lit("Summary"), col("summary")))
        if "description" in df.columns: text_parts.append(concat_ws(": ", lit("Description"), clean_html_udf(col("description"))))
        return df.select(normalize_whitespace_udf(concat_ws("\n\n", *text_parts)).alias("text"))

    def process_documents(self, path):
        data = []
        doc_path = os.path.join(path, "documents")
        if not os.path.exists(doc_path): return None
        
        # Import parsers locally
        try:
            import docx
            from pypdf import PdfReader
        except ImportError:
            print("[WARN] Doc parsers missing.")
            return None

        for f in os.listdir(doc_path):
            full_path = os.path.join(doc_path, f)
            try:
                if f.endswith(".docx"):
                    doc = docx.Document(full_path)
                    text = "\n".join([p.text for p in doc.paragraphs])
                    data.append({"text": f"### Document: {f}\n{text}"})
                elif f.endswith(".pdf"):
                    reader = PdfReader(full_path)
                    text = "\n".join([p.extract_text() or "" for p in reader.pages])
                    data.append({"text": f"### Document: {f}\n{text}"})
            except Exception as e:
                print(f"  [WARN] Failed to read {f}: {e}")
        
        if not data: return None
        return self.spark.createDataFrame(data)

    def process_all(self, input_path, output_path, chunk_size=500, overlap=50):
        dfs = []
        print(f"[*] Processing data from: {input_path}")
        
        for source in ["git_pr", "jira", "documents"]:
            print(f"  - Cleaning {source}...")
            df = None
            if source == "git_pr": df = self.process_git_pr(input_path)
            elif source == "jira": df = self.process_jira(input_path)
            elif source == "documents": df = self.process_documents(input_path)
            
            if df: dfs.append(df)

        if not dfs:
            print("[!] No data found.")
            return

        final_df = dfs[0]
        for next_df in dfs[1:]: final_df = final_df.union(next_df)
        final_df = final_df.cache()

        # 1. Save Rough-Cleaned Corpus (for SFT Refinement)
        print("[*] Collecting rough-cleaned corpus...")
        results = [{"text": row.text} for row in final_df.collect()]
        
        washed_file = os.path.join(output_path, "cleaned_corpus.jsonl")
        os.makedirs(output_path, exist_ok=True)
        with open(washed_file, 'w', encoding='utf-8') as f:
            for item in results:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"[SUCCESS] Saved {len(results)} rough-cleaned records to {washed_file}")

        # 2. Generate RAG Chunks (Simple sliding window in Spark)
        print("[*] Generating RAG chunks...")
        
        def chunk_text(text):
            if not text: return []
            chunks = []
            start = 0
            while start < len(text):
                end = start + chunk_size
                chunks.append(text[start:end])
                if end >= len(text): break
                start += (chunk_size - overlap)
            return chunks

        chunk_udf = udf(chunk_text, StringType()) # This would need ArrayType(StringType()) for proper Spark
        # For simplicity in this standalone script, we'll do it on the collected results
        # as the data scale for now is manageable on driver. 
        # If it grows, we'll move this to a proper Spark explode() transformation.
        
        rag_chunks = []
        for row in results:
            text = row["text"]
            chunks = chunk_text(text)
            for i, chunk in enumerate(chunks):
                rag_chunks.append({
                    "text": chunk,
                    "metadata": {"chunk_id": i, "source": "spark_etl"}
                })
        
        rag_file = os.path.join(output_path, "rag_chunks.jsonl")
        with open(rag_file, 'w', encoding='utf-8') as f:
            for item in rag_chunks:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"[SUCCESS] Saved {len(rag_chunks)} RAG chunks to {rag_file}")

    def stop(self):
        self.spark.stop()

def main():
    parser = argparse.ArgumentParser(description="Standalone Spark ETL")
    parser.add_argument("--input", default=DEFAULT_INPUT_PATH, help="Path to raw data")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="Path to save output")
    args = parser.parse_args()

    engine = SparkEngine()
    try:
        engine.process_all(args.input, args.output)
    finally:
        engine.stop()

if __name__ == "__main__":
    main()

