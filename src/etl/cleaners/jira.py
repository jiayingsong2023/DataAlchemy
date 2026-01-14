"""
Jira Issue Processor (Spark Version)
Uses Spark native I/O for S3 support.
"""
import json
from pyspark.sql.functions import col, concat_ws, lit
from pyspark.sql.utils import AnalysisException
from cleaners.base import clean_html_udf, normalize_whitespace_udf
from sanitizers import sanitize_udf


def process_jira(spark, path):
    """
    Process Jira issue data using Spark native reader.
    Expected schema: {key, summary, description, status, comments: []}
    """
    try:
        try:
            df = spark.read.json(path)
        except AnalysisException:
            print(f"  [WARN] Path not found or empty: {path}")
            return None
        except Exception as e:
            print(f"  [WARN] Error reading path {path}: {e}")
            return None
            
        if df.rdd.isEmpty():
            return None
        
        # Build text column with available fields
        text_parts = [lit("### Jira Issue")]
        
        # Check for columns existence before using them
        if "key" in df.columns:
            text_parts.append(concat_ws(": ", lit("Key"), col("key")))
        if "summary" in df.columns:
            text_parts.append(concat_ws(": ", lit("Summary"), col("summary")))
        if "description" in df.columns:
            text_parts.append(concat_ws(": ", lit("Description"), clean_html_udf(col("description"))))
        
        processed_df = df.select(
            concat_ws("\n\n", *text_parts).alias("raw_text"),
            (col("priority") if "priority" in df.columns else lit(0)).alias("priority_val"),
            (col("comment_count") if "comment_count" in df.columns else lit(0)).alias("comment_count")
        )
        
        final_df = processed_df.select(
            sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text"),
            col("priority_val").cast("int"),
            col("comment_count").cast("int")
        )
        
        return final_df
    except Exception as e:
        print(f"Error processing Jira data: {e}")
        return None

