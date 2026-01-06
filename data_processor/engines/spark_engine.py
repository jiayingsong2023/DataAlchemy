import os
import json
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
                dfs.append(df)

        if not dfs:
            print("[!] No data found.")
            return

        final_df = dfs[0]
        for next_df in dfs[1:]:
            final_df = final_df.union(next_df)
        
        # 1. Save Rough-Cleaned Corpus
        print("[*] Saving rough-cleaned corpus...")
        cleaned_output = join_path(output_path, "cleaned_corpus.jsonl")
        
        # Write directly using Spark
        final_df.write.mode("overwrite").json(cleaned_output)
        print(f"[SUCCESS] Saved rough-cleaned records to {cleaned_output}")

        # 2. Generate RAG Chunks
        print("[*] Generating RAG chunks...")
        
        # Define UDF for chunking
        @udf(returnType=ArrayType(StringType()))
        def chunk_text_udf(text):
            if not text: return []
            chunks = []
            start = 0
            while start < len(text):
                end = start + chunk_size
                chunks.append(text[start:end])
                if end >= len(text): break
                start += (chunk_size - overlap)
            return chunks

        rag_df = final_df.select(
            explode(chunk_text_udf(col("text"))).alias("text")
        ).withColumn("metadata", 
                     udf(lambda: {"source": "data_processor"}, StructType([StructField("source", StringType())]))()
        )
        
        # Add chunk_id
        # Note: monotonically_increasing_id is not consecutive, but unique. 
        # For strict 0..N indexing we might need zipWithIndex but that's expensive.
        # Using a simple window or just ID is usually enough.
        # Here we just save the text and metadata.
        
        rag_output = join_path(output_path, "rag_chunks.jsonl")
        rag_df.write.mode("overwrite").json(rag_output)
        
        print(f"[SUCCESS] Saved RAG chunks to {rag_output}")

    def stop(self):
        self.spark.stop()

