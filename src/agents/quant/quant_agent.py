import polars as pl
from typing import List, Optional
import os
from utils.logger import logger

from .utils import scan_parquet_optimized, get_storage_options

class QuantAgent:
    """
    Quant Agent: Handles high-dimensional feature generation.
    Optimized for memory efficiency using Polars Lazy API and Streaming.
    """
    
    def __init__(self, batch_size: int = 100000):
        self.batch_size = batch_size
        logger.info(f"QuantAgent initialized with batch_size={batch_size}")

    def generate_poly_features(self, input_path: str, output_path: str, columns: List[str], degree: int = 2):
        """
        Generate polynomial features using Polars Lazy API.
        """
        logger.info(f"Generating polynomial features (degree={degree}) from {input_path}")
        
        # 1. Use optimized scan
        if input_path.endswith(".parquet"):
            lf = scan_parquet_optimized(input_path)
        else:
            lf = pl.scan_csv(input_path)

        # 2. Define polynomial transformations lazily
        poly_exprs = []
        for col in columns:
            for d in range(2, degree + 1):
                poly_exprs.append(pl.col(col).pow(d).alias(f"{col}_pow_{d}"))

        # 3. Execute with streaming
        lf = lf.with_columns(poly_exprs)
        
        # Ensure output directory exists (if local)
        if not (output_path.startswith("s3://") or output_path.startswith("s3a://")):
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        # Sink to parquet to avoid loading everything into RAM
        if output_path.startswith("s3://") or output_path.startswith("s3a://"):
            output_path = output_path.replace("s3a://", "s3://")
            lf.sink_parquet(output_path, cloud_options=get_storage_options())
        else:
            lf.sink_parquet(output_path)
        logger.info(f"Polynomial features saved to {output_path}")

    def generate_interaction_terms(self, input_path: str, output_path: str, columns: List[str]):
        """
        Generate interaction terms (col_a * col_b) with streaming support.
        """
        logger.info(f"Generating interaction terms for {len(columns)} columns")
        
        if input_path.endswith(".parquet"):
            lf = scan_parquet_optimized(input_path)
        else:
            lf = pl.scan_csv(input_path)

        interaction_exprs = []
        for i in range(len(columns)):
            for j in range(i + 1, len(columns)):
                col_a = columns[i]
                col_b = columns[j]
                interaction_exprs.append(
                    (pl.col(col_a) * pl.col(col_b)).alias(f"inter_{col_a}_{col_b}")
                )

        lf = lf.with_columns(interaction_exprs)
        
        # Use collect(streaming=True) if complex operations are needed, 
        # but for simple multiplications sink_parquet is better.
        if output_path.startswith("s3://") or output_path.startswith("s3a://"):
            output_path = output_path.replace("s3a://", "s3://")
            lf.sink_parquet(output_path, cloud_options=get_storage_options())
        else:
            lf.sink_parquet(output_path)
        logger.info(f"Interaction terms saved to {output_path}")

    def process_in_chunks(self, lf: pl.LazyFrame, transform_fn, output_path: str):
        """
        Generic helper to process large LazyFrames and sink them.
        """
        transformed_lf = transform_fn(lf)
        transformed_lf.sink_parquet(output_path)
