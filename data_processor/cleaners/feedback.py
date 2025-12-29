import json
import os
from pyspark.sql.functions import col, concat_ws, lit
from cleaners.base import normalize_whitespace_udf
from sanitizers import sanitize_udf

def process_feedback(spark, path):
    """
    Process user feedback data.
    path: usually the raw data root (data/raw), will check for 'feedback' subfolder or parallel folder.
    """
    try:
        # Try path/feedback first, then path/../feedback (since feedback is parallel to raw)
        feedback_path = os.path.join(path, "feedback")
        if not os.path.exists(feedback_path):
            feedback_path = os.path.join(os.path.dirname(path), "feedback")
        
        print(f"    (Reading feedback from: {feedback_path})")
        
        if not os.path.exists(feedback_path):
            print(f"    [WARN] Feedback directory not found.")
            return None
            
        data = []
        for f in os.listdir(feedback_path):
            if f.endswith('.json'):
                try:
                    with open(os.path.join(feedback_path, f), 'r', encoding='utf-8') as file:
                        item = json.load(file)
                        if item.get("feedback") == "good":
                            query = item.get("query", "")
                            answer = item.get("answer", "")
                            data.append({
                                "query": query,
                                "answer": answer
                            })
                except Exception as e:
                    print(f"  [WARN] Failed to read feedback {f}: {e}")
        
        if not data:
            print(f"    [INFO] No 'good' feedback records found.")
            return None
            
        print(f"    [SUCCESS] Found {len(data)} 'good' feedback records.")
        df = spark.createDataFrame(data)
        
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
