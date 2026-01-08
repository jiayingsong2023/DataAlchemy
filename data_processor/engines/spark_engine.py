import os
import json
import time
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, udf, explode, monotonically_increasing_id, lit
from pyspark.sql.types import ArrayType, StringType, StructType, StructField, IntegerType

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
        
        # Add S3 dependencies
        # Note: Jars are now baked into the image, so no need for runtime download
        # builder = builder.config("spark.jars.packages", ...)
        
        
        # Kubernetes specific configurations
        if self.master.startswith("k8s://"):
            # The container image must be available to the K8s cluster
            image = os.environ.get("SPARK_IMAGE", "data-processor:latest")
            print(f"[*] Configuring Spark on K8s with 2 specialized executor pods for scaling...")
            builder = builder \
                .config("spark.kubernetes.container.image", image) \
                .config("spark.kubernetes.container.image.pullPolicy", "Never") \
                .config("spark.kubernetes.authenticate.driver.serviceAccountName", "spark") \
                .config("spark.executor.instances", "2") \
                .config("spark.kubernetes.namespace", "default")
        
        # S3 Configuration
        aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID")
        aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
        s3_endpoint = os.environ.get("S3_ENDPOINT")
        
        if aws_access_key and aws_secret_key:
            print("[*] Configuring S3 access and performance optimizations...")
            builder = builder \
                .config("spark.hadoop.fs.s3a.access.key", aws_access_key) \
                .config("spark.hadoop.fs.s3a.secret.key", aws_secret_key) \
                .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem") \
                .config("spark.hadoop.fs.s3a.path.style.access", "true") \
                .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "false") \
                .config("spark.hadoop.fs.s3a.committer.name", "magic") \
                .config("spark.hadoop.fs.s3a.committer.magic.enabled", "true") \
                .config("spark.hadoop.mapreduce.outputcommitter.factory.scheme.s3a", "org.apache.hadoop.fs.s3a.commit.S3ACommitterFactory")
            
            if s3_endpoint:
                builder = builder.config("spark.hadoop.fs.s3a.endpoint", s3_endpoint)

        self.spark = builder \
            .config("spark.ui.showConsoleProgress", "false") \
            .getOrCreate()
            
        # Set log level based on environment variable (default to ERROR)
        log_level = os.environ.get("LOG_LEVEL", "ERROR").upper()
        self.spark.sparkContext.setLogLevel(log_level)

    def process_all(self, input_path, output_path, chunk_size=500, overlap=50):
        dfs = []
        print(f"[*] Processing data from: {input_path}")
        
        # Mapping sources to their processor functions
        # Note: input_path is usually data/raw or s3a://bucket/data/raw
        # We assume standard structure under input_path
        
        # Helper to join paths safely for both local and S3
        def join_path(base, sub):
            if base.endswith("/"):
                return f"{base}{sub}"
            return f"{base}/{sub}"

        source_configs = [
            ("git_pr", join_path(input_path, "git_pr"), process_git_pr),
            ("jira", join_path(input_path, "jira"), process_jira),
            ("documents", join_path(input_path, "documents"), process_documents),
            ("confluence", join_path(input_path, "confluence"), process_confluence),
            ("feedback", input_path, process_feedback) # feedback logic handles its own path resolution
        ]

        for source_name, source_path, processor in source_configs:
            print(f"  - Cleaning {source_name} from {source_path}...")
            df = processor(self.spark, source_path)
            if df:
                # Add source metadata column before union
                df = df.withColumn("source_name", lit(source_name))
                dfs.append(df)

        if not dfs:
            print("[!] No data found.")
            return

        final_df = dfs[0]
        for next_df in dfs[1:]:
            # Ensure columns match for union
            final_df = final_df.unionByName(next_df)
        
        # 1. Save Rough-Cleaned Corpus
        print("[*] Saving rough-cleaned corpus...")
        cleaned_output = join_path(output_path, "cleaned_corpus.jsonl")
        
        # Write directly using Spark
        final_df.write.mode("overwrite").json(cleaned_output)
        print(f"[SUCCESS] Saved rough-cleaned records to {cleaned_output}")

        # 2. Generate RAG Chunks
        print("[*] Generating RAG chunks (Sentence-Aware Sliding Window)...")
        
        # Define UDF for sentence-aware chunking with sliding window
        @udf(returnType=ArrayType(StringType()))
        def chunk_text_udf(text):
            if not text: return []
            import re
            
            # 1. Split into sentences using improved regex
            # This pattern captures the delimiter with the sentence
            # Support for Chinese periods, exclamation, question marks and English equivalents
            sentence_pattern = r'([^。！？.!?\n]+[。！？.!?\n]*)'
            sentences = re.findall(sentence_pattern, text)
            
            if not sentences:
                # Fallback for very short text or text without markers
                if len(text) > chunk_size:
                    return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size - overlap)]
                return [text]
            
            chunks = []
            current_chunk_sentences = []
            current_len = 0
            
            for s in sentences:
                s = s.strip()
                if not s: continue
                s_len = len(s)
                
                # Case: Single sentence is too long - split it manually
                if s_len > chunk_size:
                    # Flush current
                    if current_chunk_sentences:
                        chunks.append(" ".join(current_chunk_sentences))
                        current_chunk_sentences = []
                        current_len = 0
                    # Split long sentence with overlap
                    for i in range(0, s_len, chunk_size - overlap):
                        chunks.append(s[i:i+chunk_size])
                    continue
                
                # Case: Adding this sentence exceeds chunk_size
                if current_len + s_len > chunk_size and current_chunk_sentences:
                    chunks.append(" ".join(current_chunk_sentences))
                    
                    # Sliding Window Overlap logic:
                    # Keep some sentences from previous chunk for context
                    new_chunk_sentences = []
                    new_len = 0
                    for prev_s in reversed(current_chunk_sentences):
                        if new_len + len(prev_s) < overlap:
                            new_chunk_sentences.insert(0, prev_s)
                            new_len += len(prev_s)
                        else:
                            break
                    
                    current_chunk_sentences = new_chunk_sentences + [s]
                    current_len = new_len + s_len
                else:
                    current_chunk_sentences.append(s)
                    current_len += s_len
            
            if current_chunk_sentences:
                chunks.append(" ".join(current_chunk_sentences))
                
            return chunks

        # Extract chunks and preserve metadata
        rag_df = final_df.select(
            col("source_name"),
            explode(chunk_text_udf(col("text"))).alias("text")
        ).withColumn("metadata", 
            udf(lambda s: {
                "source": s, 
                "engine": "spark_v3_sentence_aware",
                "processed_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }, StructType([
                StructField("source", StringType()),
                StructField("engine", StringType()),
                StructField("processed_at", StringType())
            ]))(col("source_name"))
        )
        
        rag_output = join_path(output_path, "rag_chunks.jsonl")
        rag_df.write.mode("overwrite").json(rag_output)
        
        print(f"[SUCCESS] Saved RAG chunks to {rag_output}")

    def stop(self):
        self.spark.stop()

