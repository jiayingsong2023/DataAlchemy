import json
import os
from pyspark.sql.functions import col, concat_ws, lit
from cleaners.base import clean_html_udf, normalize_whitespace_udf
from sanitizers import sanitize_udf

def process_git_pr(spark, path):
    """Process Git PR data using Python for reading to bypass Hadoop issues."""
    try:
        data = []
        for f in os.listdir(path):
            if f.endswith('.json'):
                full_path = os.path.join(path, f)
                with open(full_path, 'r', encoding='utf-8') as file:
                    for line in file:
                        if line.strip():
                            data.append(json.loads(line))
        
        if not data:
            return None

        # Convert local list to Spark DataFrame - This bypasses Hadoop's listStatus
        df = spark.createDataFrame(data)
        
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
