from typing import Dict, List, Optional

import polars as pl
from pydantic import BaseModel

from utils.logger import logger

from .utils import scan_parquet_optimized


class DataSchema(BaseModel):
    """
    Lightweight metadata object for cross-agent communication.
    Instead of passing data, we pass this schema.
    """
    path: str
    columns: List[str]
    dtypes: Dict[str, str]
    row_count: int
    stats_summary: Optional[Dict[str, Dict[str, float]]] = None

class ScoutAgent:
    """
    Scout Agent: Scans data sources, infers schema and collects stats.
    Focuses on 'Lazy Loading' and minimal memory footprint.
    """

    def scan_source(self, path: str) -> DataSchema:
        logger.info(f"Scouting data source: {path}")

        # 1. Lazy Scan
        if path.endswith(".parquet") or "metrics.parquet" in path:
            lf = scan_parquet_optimized(path)
        else:
            lf = pl.scan_csv(path)

        # 2. Extract metadata without loading data
        schema_dict = lf.collect_schema()
        columns = schema_dict.names()
        dtypes = {name: str(dtype) for name, dtype in schema_dict.items()}

        # 3. Quick row count
        row_count = lf.select(pl.len()).collect().item()

        # 4. Optional: collect stats on a small sample
        sample_df = lf.slice(0, 1000).collect()
        stats = {}
        for col in columns:
            if dtypes[col] in ["Int64", "Float64", "Int32", "Float32"]:
                stats[col] = {
                    "mean": float(sample_df[col].mean()) if sample_df[col].mean() is not None else 0.0,
                    "std": float(sample_df[col].std()) if sample_df[col].std() is not None else 0.0,
                    "min": float(sample_df[col].min()) if sample_df[col].min() is not None else 0.0,
                    "max": float(sample_df[col].max()) if sample_df[col].max() is not None else 0.0
                }

        schema = DataSchema(
            path=path,
            columns=columns,
            dtypes=dtypes,
            row_count=row_count,
            stats_summary=stats
        )

        logger.info(f"Scouting complete. Rows: {row_count}, Columns: {len(columns)}")
        return schema
