"""
Confluence Page Processor (Spark Version)
Uses Spark native I/O for S3 support.
"""
from cleaners.base import clean_html_udf, normalize_whitespace_udf
from pyspark.sql.functions import col, concat_ws, lit
from pyspark.sql.utils import AnalysisException
from sanitizers import sanitize_udf


def process_confluence(spark, path):
    """
    Process Confluence page data using Spark native reader.
    Expected schema: {title, body, space, created_at}
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

