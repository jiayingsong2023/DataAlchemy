from pyspark.sql.functions import col, concat_ws, lit
from spark_etl.cleaners.base import clean_html_udf, normalize_whitespace_udf
from spark_etl.sanitizers import sanitize_udf

def process_git_pr(spark, path):
    """
    Process Git PR data.
    Expected schema: {id, title, description, author, comments: [{body, author}], diff_summary}
    """
    try:
        df = spark.read.json(path)
        
        # Combine title and description
        # We also sanitize and clean HTML
        processed_df = df.select(
            concat_ws(
                "\n\n",
                lit("### Git PR Source"),
                concat_ws(": ", lit("Title"), col("title")),
                concat_ws(": ", lit("Description"), clean_html_udf(col("description"))),
                concat_ws(": ", lit("Diff Summary"), col("diff_summary"))
            ).alias("raw_text")
        )
        
        # Apply final cleaning and sanitization
        final_df = processed_df.select(
            sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text")
        )
        
        return final_df
    except Exception as e:
        print(f"Error processing Git PR data: {e}")
        return None
