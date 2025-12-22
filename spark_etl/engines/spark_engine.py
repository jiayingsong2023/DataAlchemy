"""
Spark Distributed Data Processing Engine
- Full PySpark power for large-scale data processing
- Optimized for: Linux servers, EMR, Databricks, cloud environments
- Data scale: 1GB - 100GB+ (multi-node cluster recommended for very large datasets)

NOTE: This engine uses Python I/O for file reading to bypass Hadoop native library
issues on Windows. For true distributed file reading in production, configure
HDFS, S3, or other distributed storage and use spark.read.json() directly.
"""
import os
import json
import io
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, concat_ws, lit

# Import PDF/DOCX parsers
try:
    import docx
    from pypdf import PdfReader
    HAS_DOC_PARSERS = True
except ImportError:
    HAS_DOC_PARSERS = False

from spark_etl.config import (
    SPARK_APP_NAME, SPARK_MASTER,
    GIT_PR_PATH, JIRA_PATH, CONFLUENCE_PATH, DOCUMENTS_PATH
)
from spark_etl.cleaners.base import clean_html_udf, normalize_whitespace_udf
from spark_etl.sanitizers import sanitize_udf


class SparkEngine:
    """Spark-based processing engine for large-scale distributed data."""
    
    def __init__(self):
        self.engine_name = "SparkEngine"
        print(f"[{self.engine_name}] Initializing Spark Session...")
        
        # Configure Spark for local file system compatibility
        self.spark = SparkSession.builder \
            .appName(SPARK_APP_NAME) \
            .master(SPARK_MASTER) \
            .config("spark.ui.showConsoleProgress", "false") \
            .getOrCreate()
        
        # Reduce logging verbosity
        self.spark.sparkContext.setLogLevel("WARN")
        print(f"[{self.engine_name}] Spark Session initialized (Master: {SPARK_MASTER})")
    
    # --- File Reading Helpers (bypass Hadoop for local files) ---
    def _read_json_files_to_list(self, path: str) -> list:
        """Read JSON/JSONL files using native Python (avoids Hadoop issues)."""
        data = []
        if not os.path.exists(path):
            return data
        for f in os.listdir(path):
            if not f.endswith('.json'):
                continue
            full_path = os.path.join(path, f)
            try:
                with open(full_path, 'r', encoding='utf-8') as file:
                    for line in file:
                        if line.strip():
                            data.append(json.loads(line))
            except Exception as e:
                print(f"  [WARN] Error reading {f}: {e}")
        return data
    
    def _read_documents_to_list(self, path: str) -> list:
        """Read binary documents using native Python."""
        data = []
        if not os.path.exists(path) or not HAS_DOC_PARSERS:
            return data
        
        for f in os.listdir(path):
            full_path = os.path.join(path, f)
            if not os.path.isfile(full_path):
                continue
            
            try:
                with open(full_path, 'rb') as file:
                    content = file.read()
                
                if f.lower().endswith(".docx"):
                    doc = docx.Document(io.BytesIO(content))
                    text = "\n".join([para.text for para in doc.paragraphs])
                    source_type = "DOCX"
                elif f.lower().endswith(".pdf"):
                    reader = PdfReader(io.BytesIO(content))
                    text = "\n".join([page.extract_text() or "" for page in reader.pages])
                    source_type = "PDF"
                else:
                    continue
                
                if text.strip():
                    data.append({
                        "file_name": f,
                        "source_type": source_type,
                        "raw_content": text
                    })
            except Exception as e:
                print(f"  [WARN] Error reading {f}: {e}")
        return data
    
    # --- Data Source Processors ---
    def _process_git_pr(self) -> "DataFrame":
        """Process Git PR data with Spark UDFs."""
        data = self._read_json_files_to_list(GIT_PR_PATH)
        if not data:
            return None
        
        df = self.spark.createDataFrame(data)
        
        # Build text column with available fields
        text_parts = [lit("### Git PR Source")]
        if "title" in df.columns:
            text_parts.append(concat_ws(": ", lit("Title"), col("title")))
        if "description" in df.columns:
            text_parts.append(concat_ws(": ", lit("Description"), clean_html_udf(col("description"))))
        if "diff_summary" in df.columns:
            text_parts.append(concat_ws(": ", lit("Diff Summary"), col("diff_summary")))
        
        processed_df = df.select(
            concat_ws("\n\n", *text_parts).alias("raw_text")
        )
        
        return processed_df.select(
            sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text")
        )
    
    def _process_jira(self) -> "DataFrame":
        """Process Jira issue data with Spark UDFs."""
        data = self._read_json_files_to_list(JIRA_PATH)
        if not data:
            return None
        
        df = self.spark.createDataFrame(data)
        
        text_parts = [lit("### Jira Issue")]
        if "key" in df.columns:
            text_parts.append(concat_ws(": ", lit("Key"), col("key")))
        if "summary" in df.columns:
            text_parts.append(concat_ws(": ", lit("Summary"), col("summary")))
        if "description" in df.columns:
            text_parts.append(concat_ws(": ", lit("Description"), clean_html_udf(col("description"))))
        
        processed_df = df.select(
            concat_ws("\n\n", *text_parts).alias("raw_text")
        )
        
        return processed_df.select(
            sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text")
        )
    
    def _process_confluence(self) -> "DataFrame":
        """Process Confluence page data with Spark UDFs."""
        data = self._read_json_files_to_list(CONFLUENCE_PATH)
        if not data:
            return None
        
        df = self.spark.createDataFrame(data)
        
        text_parts = [lit("### Confluence Page")]
        if "title" in df.columns:
            text_parts.append(concat_ws(": ", lit("Title"), col("title")))
        if "body" in df.columns:
            text_parts.append(concat_ws(": ", lit("Content"), clean_html_udf(col("body"))))
        
        processed_df = df.select(
            concat_ws("\n\n", *text_parts).alias("raw_text")
        )
        
        return processed_df.select(
            sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text")
        )
    
    def _process_documents(self) -> "DataFrame":
        """Process PDF/DOCX documents with Spark UDFs."""
        data = self._read_documents_to_list(DOCUMENTS_PATH)
        if not data:
            return None
        
        df = self.spark.createDataFrame(data)
        
        processed_df = df.select(
            concat_ws(
                "\n\n",
                lit("### Document Source"),
                concat_ws(": ", lit("File"), col("file_name")),
                concat_ws(": ", lit("Type"), col("source_type")),
                concat_ws(": ", lit("Content"), col("raw_content"))
            ).alias("raw_text")
        )
        
        return processed_df.select(
            sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text")
        )
    
    # --- Main Processing Method ---
    def process_all(self) -> list:
        """Process all data sources and return combined results."""
        dataframes = []
        
        # 1. Git PRs
        print("\n[1/4] Processing Git PRs...")
        df = self._process_git_pr()
        if df:
            count = df.count()
            print(f"  Found {count} records.")
            dataframes.append(df)
        else:
            print("  No data found.")
        
        # 2. Jira
        print("[2/4] Processing Jira Issues...")
        df = self._process_jira()
        if df:
            count = df.count()
            print(f"  Found {count} records.")
            dataframes.append(df)
        else:
            print("  No data found.")
        
        # 3. Confluence
        print("[3/4] Processing Confluence Pages...")
        df = self._process_confluence()
        if df:
            count = df.count()
            print(f"  Found {count} records.")
            dataframes.append(df)
        else:
            print("  No data found.")
        
        # 4. Documents
        print("[4/4] Processing Binary Documents (PDF/DOCX)...")
        df = self._process_documents()
        if df:
            count = df.count()
            print(f"  Found {count} records.")
            dataframes.append(df)
        else:
            print("  No data found.")
        
        if not dataframes:
            return []
        
        # Union all DataFrames
        final_df = dataframes[0]
        for df in dataframes[1:]:
            final_df = final_df.union(df)
        
        # Collect to Python list (avoids Hadoop write committers)
        print("\n[Spark] Collecting results to driver...")
        results = [{"text": row.text} for row in final_df.collect()]
        return results
    
    def stop(self):
        """Stop Spark session."""
        if self.spark:
            self.spark.stop()
            print(f"[{self.engine_name}] Spark Session stopped.")

