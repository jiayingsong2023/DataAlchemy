from pyspark.ml.feature import HashingTF, MinHashLSH, Tokenizer
from pyspark.sql import functions as F

from utils.logger import logger


class MinHashDedup:
    """
    MinHash LSH based semantic deduplication for Spark DataFrames.
    Identifies and removes near-duplicate documents using Jaccard similarity.
    """

    def __init__(self, threshold: float = 0.9, num_hash_tables: int = 5, input_col: str = "text"):
        """
        Args:
            threshold (float): Similarity threshold (0.0 to 1.0). Default 0.9.
            num_hash_tables (int): Number of hash tables for LSH. Higher is more accurate but slower.
            input_col (str): The column containing text to deduplicate.
        """
        self.threshold = threshold
        self.num_hash_tables = num_hash_tables
        self.input_col = input_col

    def deduplicate(self, df):
        """
        Performs semantic deduplication on the input DataFrame.
        Returns a deduplicated DataFrame.
        """
        if df is None:
            return None

        logger.info(
            f"Starting MinHash LSH deduplication (threshold={self.threshold}, tables={self.num_hash_tables})"
        )

        # Ensure we have a unique ID for deduplication logic
        if "id" not in df.columns:
            df = df.withColumn("id", F.monotonically_increasing_id())

        original_count = df.count()
        if original_count == 0:
            return df

        # 1. Tokenize text into words
        tokenizer = Tokenizer(inputCol=self.input_col, outputCol="tokens")
        words_df = tokenizer.transform(df)

        # 2. Convert tokens to feature vectors
        # Using 1024 features to balance between precision and performance
        hashing_tf = HashingTF(inputCol="tokens", outputCol="features", numFeatures=1024)
        featurized_df = hashing_tf.transform(words_df)

        # 3. Apply MinHash LSH
        mh = MinHashLSH(inputCol="features", outputCol="hashes", numHashTables=self.num_hash_tables)
        model = mh.fit(featurized_df)

        # 4. Identify duplicates using approxSimilarityJoin
        # Jaccard Distance = 1 - Jaccard Similarity
        # We find pairs where distance <= (1 - threshold)
        dist_threshold = 1.0 - self.threshold

        logger.info(
            f"Running approximate similarity join with distance threshold {dist_threshold:.2f}..."
        )

        # Self-join to finding duplicates
        # Optimization: We only care about pairs where ID_A < ID_B to avoid self-matches and half of the redundant pairs
        similar_pairs = model.approxSimilarityJoin(
            featurized_df, featurized_df, dist_threshold, distCol="JaccardDistance"
        ).filter("datasetA.id < datasetB.id")

        # 5. Extract IDs that should be removed
        # If A and B are similar, we keep A (smaller ID) and drop B
        ids_to_drop = similar_pairs.select(F.col("datasetB.id")).distinct()

        # 6. Filter out duplicates
        deduplicated_df = df.join(ids_to_drop, df.id == ids_to_drop.id, "left_anti")

        final_count = deduplicated_df.count()
        removed_count = original_count - final_count

        logger.info(
            f"Deduplication complete: {original_count} -> {final_count} (Removed {removed_count} near-duplicates)"
        )

        return deduplicated_df.drop("id")  # Clean up temporary ID if we added it


# Standalone testing helper
if __name__ == "__main__":
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.appName("TestDedup").getOrCreate()

    test_data = [
        (1, "The quick brown fox jumps over the lazy dog"),
        (2, "The quick brown fox jumped over the lazy dog"),  # Near duplicate
        (3, "I love programming in Python"),
        (4, "This is a completely different sentence"),
        (5, "I love coding in Python"),  # Near duplicate
    ]
    test_df = spark.createDataFrame(test_data, ["id", "text"])

    deduper = MinHashDedup(threshold=0.7)
    result = deduper.deduplicate(test_df)
    result.show(truncate=False)
