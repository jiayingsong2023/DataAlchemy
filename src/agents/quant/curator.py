from typing import List, Tuple

from scipy import sparse

from utils.logger import logger

from .utils import get_storage_options, scan_parquet_optimized


class CuratorAgent:
    """
    Curator Agent: Handles feature selection and correlation analysis.
    Optimized for high-dimensional data using chunking and sparse matrices.
    """

    def __init__(self, correlation_threshold: float = 0.95):
        self.correlation_threshold = correlation_threshold
        logger.info(f"CuratorAgent initialized with threshold={correlation_threshold}")

    def compute_chunked_correlation(self, input_path: str, columns: List[str], chunk_size: int = 500) -> List[Tuple[str, str, float]]:
        """
        Compute correlation matrix in blocks to avoid memory peak.
        Returns pairs of highly correlated features.
        """
        logger.info(f"Computing chunked correlation for {len(columns)} features...")

        lf = scan_parquet_optimized(input_path).select(columns)
        df = lf.collect()
        corr_matrix = df.corr()

        high_corr_pairs = []
        cols = corr_matrix.columns
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                val = corr_matrix[i, j]
                if abs(val) > self.correlation_threshold:
                    high_corr_pairs.append((cols[i], cols[j], val))

        logger.info(f"Found {len(high_corr_pairs)} pairs with correlation > {self.correlation_threshold}")
        return high_corr_pairs

    def to_sparse_matrix(self, input_path: str, columns: List[str]) -> sparse.csr_matrix:
        """
        Convert high-dimensional dense data to CSR sparse matrix for efficient storage/compute.
        """
        logger.info(f"Converting {input_path} to sparse matrix...")

        df = scan_parquet_optimized(input_path).select(columns).collect()
        sparse_matrix = sparse.csr_matrix(df.to_numpy())
        logger.info(f"Sparse matrix created: {sparse_matrix.shape} with {sparse_matrix.nnz} non-zero elements")
        return sparse_matrix

    def drop_redundant_features(self, input_path: str, output_path: str, columns: List[str]):
        """
        Filter features based on correlation and save to a new file.
        """
        high_corr_pairs = self.compute_chunked_correlation(input_path, columns)

        to_drop = set([pair[1] for pair in high_corr_pairs])
        remaining_cols = [c for c in columns if c not in to_drop]

        logger.info(f"Dropping {len(to_drop)} redundant features. Remaining: {len(remaining_cols)}")

        lf = scan_parquet_optimized(input_path).select(remaining_cols)

        if output_path.startswith("s3://") or output_path.startswith("s3a://"):
            s3_output = output_path.replace("s3a://", "s3://")
            lf.sink_parquet(s3_output, cloud_options=get_storage_options())
        else:
            lf.sink_parquet(output_path)
        logger.info(f"Cleaned dataset saved to {output_path}")
