import json
from pyspark.sql.functions import col, concat_ws, lit
from pyspark.sql.utils import AnalysisException
from cleaners.base import clean_html_udf, normalize_whitespace_udf
from sanitizers import sanitize_udf

def process_git_pr(spark, path):
    """Process Git PR data using Spark native reader for S3 support."""
    try:
        # Read JSON files directly using Spark
        # This supports S3 paths (s3a://...)
        try:
            df = spark.read.json(path)
        except AnalysisException:
            # Path might not exist or be empty
            print(f"  [WARN] Path not found or empty: {path}")
            return None
        except Exception as e:
            print(f"  [WARN] Error reading path {path}: {e}")
            return None
            
        if df.rdd.isEmpty():
            return None
        
        # Ensure required columns exist
        required_cols = ["title", "description", "diff_summary"]
        for c in required_cols:
            if c not in df.columns:
                df = df.withColumn(c, lit(""))

        processed_df = df.select(
            concat_ws(
                "\n\n",
                lit("### Git PR Source"),
                concat_ws(": ", lit("Title"), col("title")),
                concat_ws(": ", lit("Description"), clean_html_udf(col("description"))),
                concat_ws(": ", lit("Diff Summary"), col("diff_summary"))
            ).alias("raw_text")
        )
        
        return processed_df.select(
            sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text")
        )
    except Exception as e:
        print(f"Error processing Git PR data: {e}")
        return None

