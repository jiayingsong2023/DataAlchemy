"""
Jira Issue Processor (Spark Version)
Uses Python I/O for file reading to bypass Hadoop native library issues.
"""
import json
import os
from pyspark.sql.functions import col, concat_ws, lit
from spark_etl.cleaners.base import clean_html_udf, normalize_whitespace_udf
from spark_etl.sanitizers import sanitize_udf


def process_jira(spark, path):
    """
    Process Jira issue data using Python for file reading.
    Expected schema: {key, summary, description, status, comments: []}
    """
    try:
        # Read files using native Python to bypass Hadoop
        data = []
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
        
        if not data:
            return None

        # Convert to Spark DataFrame
        df = spark.createDataFrame(data)
        
        # Build text column with available fields
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
        
        final_df = processed_df.select(
            sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text")
        )
        
        return final_df
    except Exception as e:
        print(f"Error processing Jira data: {e}")
        return None
