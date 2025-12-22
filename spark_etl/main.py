import os
import sys
import argparse
from pyspark.sql import SparkSession
from spark_etl.config import (
    SPARK_APP_NAME, SPARK_MASTER,
    GIT_PR_PATH, JIRA_PATH, CONFLUENCE_PATH, DOCUMENTS_PATH,
    FINAL_OUTPUT_PATH
)
from spark_etl.cleaners.git_pr import process_git_pr
from spark_etl.cleaners.jira import process_jira
from spark_etl.cleaners.confluence import process_confluence
from spark_etl.cleaners.document import process_documents
from spark_etl.sft_generator import SFTGenerator

def main():
    parser = argparse.ArgumentParser(description="Spark ETL and SFT Data Generation")
    parser.add_argument("--sft", action="store_true", help="Generate SFT data using LLM after Spark processing")
    parser.add_argument("--max_samples", type=int, default=None, help="Max samples for SFT generation")
    args = parser.parse_args()

    # Initialize Spark Session
    # Using local[*] for local demo mode
    spark = SparkSession.builder \
        .appName(SPARK_APP_NAME) \
        .master(SPARK_MASTER) \
        .getOrCreate()
    
    print(f"Spark Session initialized: {SPARK_APP_NAME} (Master: {SPARK_MASTER})")

    dataframes = []

    # 1. Process Git PRs
    if os.path.exists(GIT_PR_PATH) and os.listdir(GIT_PR_PATH):
        print(f"Processing Git PRs from {GIT_PR_PATH}...")
        git_df = process_git_pr(spark, GIT_PR_PATH)
        if git_df: dataframes.append(git_df)

    # 2. Process Jira Issues
    if os.path.exists(JIRA_PATH) and os.listdir(JIRA_PATH):
        print(f"Processing Jira Issues from {JIRA_PATH}...")
        jira_df = process_jira(spark, JIRA_PATH)
        if jira_df: dataframes.append(jira_df)

    # 3. Process Confluence Pages
    if os.path.exists(CONFLUENCE_PATH) and os.listdir(CONFLUENCE_PATH):
        print(f"Processing Confluence Pages from {CONFLUENCE_PATH}...")
        conf_df = process_confluence(spark, CONFLUENCE_PATH)
        if conf_df: dataframes.append(conf_df)

    # 4. Process Binary Documents (.docx, .pdf)
    if os.path.exists(DOCUMENTS_PATH) and os.listdir(DOCUMENTS_PATH):
        print(f"Processing Binary Documents from {DOCUMENTS_PATH}...")
        doc_df = process_documents(spark, DOCUMENTS_PATH)
        if doc_df: dataframes.append(doc_df)

    if not dataframes:
        print("No data found to process.")
        spark.stop()
        return

    # Union all dataframes
    final_corpus = dataframes[0]
    for df in dataframes[1:]:
        final_corpus = final_corpus.union(df)

    # Save output as JSONL (single column: text)
    # Note: Spark's .json() writes a directory of part-files.
    # For a small demo, we can coalesce(1) to get a single file.
    output_dir = os.path.join(os.path.dirname(FINAL_OUTPUT_PATH), "processed_temp")
    final_corpus.coalesce(1).write.mode("overwrite").json(output_dir)
    
    print(f"Data processing complete. Temp output saved to: {output_dir}")
    
    # Optional: Rename/move the part-file to the final destination
    # In a real cluster environment, we'd leave it as part-files.
    try:
        import glob
        part_file = glob.glob(os.path.join(output_dir, "part-*.json"))[0]
        if os.path.exists(FINAL_OUTPUT_PATH):
            os.remove(FINAL_OUTPUT_PATH)
        os.rename(part_file, FINAL_OUTPUT_PATH)
        print(f"Final corpus merged and saved to: {FINAL_OUTPUT_PATH}")
    except Exception as e:
        print(f"Could not merge part-files to final destination: {e}")

    spark.stop()

    # 5. SFT Data Generation (Optional)
    if args.sft:
        print("\n--- Starting SFT Data Generation ---")
        generator = SFTGenerator()
        generator.process_corpus(FINAL_OUTPUT_PATH, max_samples=args.max_samples)

if __name__ == "__main__":
    main()

