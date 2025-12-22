from pyspark.sql.functions import col, concat_ws, lit
from spark_etl.cleaners.base import clean_html_udf, normalize_whitespace_udf
from spark_etl.sanitizers import sanitize_udf

def process_jira(spark, path):
    """
    Process Jira issue data.
    Expected schema: {key, summary, description, status, comments: []}
    """
    try:
        df = spark.read.json(path)
        
        processed_df = df.select(
            concat_ws(
                "\n\n",
                lit("### Jira Issue"),
                concat_ws(": ", lit("Key"), col("key")),
                concat_ws(": ", lit("Summary"), col("summary")),
                concat_ws(": ", lit("Description"), clean_html_udf(col("description")))
            ).alias("raw_text")
        )
        
        final_df = processed_df.select(
            sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text")
        )
        
        return final_df
    except Exception as e:
        print(f"Error processing Jira data: {e}")
        return None

