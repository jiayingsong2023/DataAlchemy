import os

from pyspark.sql import SparkSession
from pyspark.sql.functions import lit

# Import path configuration
from config import SPARK_JARS_DIR

from ..cleaners.confluence import process_confluence
from ..cleaners.document import process_documents
from ..cleaners.feedback import process_feedback

# Import specialized cleaners relative to parent package
from ..cleaners.git_pr import process_git_pr
from ..cleaners.jira import process_jira
from ..dedup.minhash_dedup import MinHashDedup


class SparkEngine:
    def __init__(self, master=None, app_name="K8sSparkWash"):
        # Use master from env if provided, else default to local[*]
        self.master = master or os.environ.get("SPARK_MASTER", "local[*]")

        builder = SparkSession.builder.appName(app_name).master(self.master)

        # Add S3 dependencies
        # Use configurable path from config.py (supports both local dev and k3d)
        local_jars_dir = SPARK_JARS_DIR

        print(f"[*] Checking for Spark jars in: {local_jars_dir}")
        if os.path.exists(local_jars_dir):
            jar_files = [
                os.path.join(local_jars_dir, f)
                for f in os.listdir(local_jars_dir)
                if f.endswith(".jar")
            ]
            if jar_files:
                print(f"[*] Found {len(jar_files)} local Spark jars: {jar_files}")
                print("[*] Using LOCAL jars for offline mode. Maven packages will be skipped.")
                builder = builder.config("spark.jars", ",".join(jar_files))
            else:
                print("[!] No .jar files found in spark-jars directory. Falling back to Maven.")
                builder = builder.config(
                    "spark.jars.packages",
                    "org.apache.hadoop:hadoop-aws:3.3.4,com.amazonaws:aws-java-sdk-bundle:1.12.262",
                )
        else:
            print(f"[!] Spark jars directory NOT FOUND at {local_jars_dir}. Falling back to Maven.")
            builder = builder.config(
                "spark.jars.packages",
                "org.apache.hadoop:hadoop-aws:3.3.4,com.amazonaws:aws-java-sdk-bundle:1.12.262",
            )

        # Kubernetes specific configurations
        if self.master.startswith("k8s://"):
            # The container image must be available to the K8s cluster
            image = os.environ.get("SPARK_IMAGE", "data-processor:latest")
            print("[*] Configuring Spark on K8s with 2 specialized executor pods for scaling...")
            builder = (
                builder.config("spark.kubernetes.container.image", image)
                .config("spark.kubernetes.container.image.pullPolicy", "Never")
                .config("spark.kubernetes.authenticate.driver.serviceAccountName", "spark")
                .config("spark.executor.instances", "2")
                .config("spark.kubernetes.namespace", "default")
            )

        # S3 Configuration
        aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID")
        aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
        s3_endpoint = os.environ.get("S3_ENDPOINT")

        if aws_access_key and aws_secret_key:
            print("[*] Configuring S3 access and performance optimizations...")
            builder = (
                builder.config("spark.hadoop.fs.s3a.access.key", aws_access_key)
                .config("spark.hadoop.fs.s3a.secret.key", aws_secret_key)
                .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem")
                .config("spark.hadoop.fs.s3a.path.style.access", "true")
                .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "false")
                .config("spark.hadoop.fs.s3a.committer.name", "magic")
                .config("spark.hadoop.fs.s3a.committer.magic.enabled", "true")
                .config(
                    "spark.hadoop.mapreduce.outputcommitter.factory.scheme.s3a",
                    "org.apache.hadoop.fs.s3a.commit.S3ACommitterFactory",
                )
            )

            if s3_endpoint:
                builder = builder.config("spark.hadoop.fs.s3a.endpoint", s3_endpoint)

            # Explicitly set timeouts to integers to avoid "60s" string parsing issues in some Hadoop versions
            builder = (
                builder.config("spark.hadoop.fs.s3a.connection.timeout", "60000")
                .config("spark.hadoop.fs.s3a.connection.establish.timeout", "60000")
                .config("spark.hadoop.fs.s3a.attempts.maximum", "3")
            )

        self.spark = builder.config("spark.ui.showConsoleProgress", "false").getOrCreate()

        # Set log level based on environment variable (default to ERROR)
        log_level = os.environ.get("LOG_LEVEL", "ERROR").upper()
        self.spark.sparkContext.setLogLevel(log_level)

    def process_all(self, input_path, output_path):
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
            (
                "feedback",
                input_path,
                process_feedback,
            ),  # feedback logic handles its own path resolution
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
            # Ensure columns match for union, allowing missing columns for metrics
            final_df = final_df.unionByName(next_df, allowMissingColumns=True)

        # Fill nulls for numerical metrics to ensure consistency in Parquet
        num_cols = [c for c, t in final_df.dtypes if t in ("int", "double", "float", "bigint")]
        final_df = final_df.fillna(0, subset=num_cols)

        # 0. Semantic Deduplication (NEW)
        # Removes near-duplicates to improve data quality for SFT and RAG
        deduper = MinHashDedup(threshold=0.9)
        final_df = deduper.deduplicate(final_df)

        # 1. Save Rough-Cleaned Corpus (JSONL)
        print(f"[*] Saving rough-cleaned corpus to {output_path}...")
        cleaned_output = join_path(output_path, "cleaned_corpus.jsonl")

        # Preserve lineage columns.  Older source cleaners do not provide these
        # columns, so unionByName fills them with nulls; H3 document rows carry
        # source version, ACL digest, trust label and locator.
        final_df.write.mode("overwrite").json(cleaned_output)
        print(f"[SUCCESS] Saved rough-cleaned records to {cleaned_output}")

        # 2. Save Numerical Metrics (Parquet) - NEW
        if num_cols:
            print(f"[*] Saving {len(num_cols)} numerical metrics to metrics.parquet...")
            metrics_output = join_path(output_path, "metrics.parquet")
            final_df.select("source_name", *num_cols).write.mode("overwrite").parquet(
                metrics_output
            )
            print(f"[SUCCESS] Metrics saved to {metrics_output}")


    def stop(self):
        self.spark.stop()
