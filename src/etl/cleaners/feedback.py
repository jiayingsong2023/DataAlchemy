import json
from pyspark.sql.functions import col, concat_ws, lit
from pyspark.sql.utils import AnalysisException
from cleaners.base import normalize_whitespace_udf
from sanitizers import sanitize_udf

def process_feedback(spark, path):
    """
    Process user feedback data using Spark native reader.
    path: usually the raw data root (e.g. s3a://bucket/data/raw)
    """
    try:
        # Helper to join paths ensuring forward slashes
        def join_path(base, sub):
            base = base.rstrip("/")
            sub = sub.lstrip("/")
            return f"{base}/{sub}"

        # Try path/feedback first
        feedback_path = join_path(path, "feedback")
        
        # Also try sibling directory: ../feedback
        # If path ends with /raw, we want .../feedback
        sibling_path = None
        if path.rstrip("/").endswith("/raw"):
            parent = path.rstrip("/")[:-4] # remove /raw
            sibling_path = join_path(parent, "feedback")
        
        paths_to_try = [feedback_path]
        if sibling_path:
            paths_to_try.append(sibling_path)
            
        df = None
        used_path = ""
        
        for p in paths_to_try:
            try:
                print(f"    (Trying to read feedback from: {p})")
                df = spark.read.json(p)
                if not df.rdd.isEmpty():
                    used_path = p
                    break
            except AnalysisException:
                continue
            except Exception as e:
                print(f"    [WARN] Error reading {p}: {e}")
                continue
        
        if df is None:
            print(f"    [WARN] Feedback directory not found in {paths_to_try}.")
            return None
        
        # Count total records before filtering
        total_count = df.count()
        print(f"    [SUCCESS] Found feedback records in {used_path}.")
        print(f"    [INFO] Total feedback files/records read: {total_count}")
        
        # Filter for good feedback
        # Expected schema: {feedback: "good", query: "...", answer: "..."}
        
        if "feedback" not in df.columns:
            return None
            
        df = df.filter(col("feedback") == "good")
        
        good_count = df.count()
        if good_count == 0:
            print(f"    [INFO] No 'good' feedback records found.")
            return None
        
        print(f"    [INFO] Found {good_count} 'good' feedback records (filtered from {total_count} total).")
            
        # Ensure columns exist
        if "query" not in df.columns:
            df = df.withColumn("query", lit(""))
        if "answer" not in df.columns:
            df = df.withColumn("answer", lit(""))

        processed_df = df.select(
            concat_ws(
                "\n\n",
                lit("### User Feedback"),
                concat_ws(": ", lit("Question"), col("query")),
                concat_ws(": ", lit("Answer"), col("answer"))
            ).alias("raw_text")
        )
        
        return processed_df.select(
            sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text")
        )
    except Exception as e:
        print(f"Error processing feedback: {e}")
        return None

