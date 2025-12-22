from pyspark.sql.functions import col, concat_ws, lit
from spark_etl.cleaners.base import clean_html_udf, normalize_whitespace_udf
from spark_etl.sanitizers import sanitize_udf

def process_confluence(spark, path):
    """
    Process Confluence page data.
    Expected schema: {id, title, space, body, last_modified}
    """
    try:
        df = spark.read.json(path)
        
        processed_df = df.select(
            concat_ws(
                "\n\n",
                lit("### Confluence Page"),
                concat_ws(": ", lit("Title"), col("title")),
                concat_ws(": ", lit("Space"), col("space")),
                concat_ws(": ", lit("Content"), clean_html_udf(col("body")))
            ).alias("raw_text")
        )
        
        final_df = processed_df.select(
            sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text")
        )
        
        return final_df
    except Exception as e:
        print(f"Error processing Confluence data: {e}")
        return None

