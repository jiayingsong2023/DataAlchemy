"""
Confluence Page Processor (Spark Version)
Uses Python I/O for file reading to bypass Hadoop native library issues.
"""
import json
import os
from pyspark.sql.functions import col, concat_ws, lit
from etl.cleaners.base import clean_html_udf, normalize_whitespace_udf
from etl.sanitizers import sanitize_udf


def process_confluence(spark, path):
    """
    Process Confluence page data using Python for file reading.
    Expected schema: {title, body, space, created_at}
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
        text_parts = [lit("### Confluence Page")]
        if "title" in df.columns:
            text_parts.append(concat_ws(": ", lit("Title"), col("title")))
        if "body" in df.columns:
            text_parts.append(concat_ws(": ", lit("Content"), clean_html_udf(col("body"))))
        if "space" in df.columns:
            text_parts.append(concat_ws(": ", lit("Space"), col("space")))
        
        processed_df = df.select(
            concat_ws("\n\n", *text_parts).alias("raw_text")
        )
        
        final_df = processed_df.select(
            sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text")
        )
        
        return final_df
    except Exception as e:
        print(f"Error processing Confluence data: {e}")
        return None
