from typing import List

from utils.logger import logger

from .scout import DataSchema


class ValidatorAgent:
    """
    Validator Agent: Ensures data integrity based on Schema.
    Does NOT hold data copies.
    """

    def validate_schema(self, schema: DataSchema, expected_columns: List[str]) -> bool:
        logger.info(f"Validating schema for {schema.path}")
        missing = [c for c in expected_columns if c not in schema.columns]
        if missing:
            logger.error(f"Validation failed. Missing columns: {missing}")
            return False

        logger.info("Schema validation successful.")
        return True

    def check_drift(self, current_schema: DataSchema, reference_schema: DataSchema) -> bool:
        """
        Compare statistics between two schemas to detect data drift.
        Uses the lightweight Pydantic schema objects.
        """
        if not current_schema.stats_summary or not reference_schema.stats_summary:
            logger.warning("Stats missing, cannot check drift.")
            return True

        for col, stats in current_schema.stats_summary.items():
            if col in reference_schema.stats_summary:
                old_mean = reference_schema.stats_summary[col]["mean"]
                new_mean = stats["mean"]
                if old_mean and abs(new_mean - old_mean) / (abs(old_mean) + 1e-9) > 0.5:
                    logger.warning(f"Drift detected in column {col}: {old_mean} -> {new_mean}")

        return True
