"""Traceable PDF/DOCX rough cleaning for the H3 pilot."""

from __future__ import annotations

import json
from typing import Any

from pyspark.sql.functions import col, element_at, explode, lit, sha2, split, udf
from pyspark.sql.types import (
    ArrayType,
    IntegerType,
    StringType,
    StructField,
    StructType,
)
from pyspark.sql.utils import AnalysisException

from ..sanitizers import sanitize_text
from .base import normalize_whitespace

_RECORD_SCHEMA = StructType(
    [
        StructField("text", StringType(), False),
        StructField("page", IntegerType(), True),
        StructField("paragraph", IntegerType(), True),
        StructField("decision", StringType(), False),
        StructField("reason_codes", ArrayType(StringType()), False),
    ]
)
_PARSED_SCHEMA = ArrayType(_RECORD_SCHEMA)


def _parse(filename: str, content: bytes) -> list[dict[str, Any]]:
    # Imports stay inside the UDF so Spark executors do not deserialize a driver
    # parser object.  The same deterministic parser is used by the upload gate.
    from harness.product_loop import parse_document

    try:
        parsed = parse_document(content, filename)
    except Exception as error:
        return [
            {
                "text": "",
                "page": None,
                "paragraph": None,
                "decision": "rejected",
                "reason_codes": [getattr(error, "code", "document_parse_failed")],
            }
        ]
    if not parsed:
        return [
            {
                "text": "",
                "page": None,
                "paragraph": None,
                "decision": "rejected",
                "reason_codes": ["document_text_empty"],
            }
        ]
    # A page/paragraph is one rough record.  The H3 refine step combines records
    # into a document while preserving each locator.
    return [
        {
            "text": normalize_whitespace(sanitize_text(item["text"])),
            "page": item["page"],
            "paragraph": item["paragraph"],
            "decision": "quarantined" if item["injection_codes"] else "accepted",
            "reason_codes": item["injection_codes"],
        }
        for item in parsed
    ]


_parse_udf = udf(_parse, _PARSED_SCHEMA)


def _descriptor(spark: Any, path: str) -> dict[str, Any]:
    """Read the exact run input descriptor; missing legacy descriptors are explicit."""
    try:
        rows = spark.read.json(f"{path.rstrip('/')}/input.json").limit(1).collect()
    except Exception:
        return {
            "schema_version": 1,
            "tenant_id": "legacy",
            "input_id": "legacy",
            "source": {"version": "sha256:unknown"},
            "acl": [],
            "acl_digest": "unknown",
            "trust_label": "legacy_unverified",
        }
    return rows[0].asDict(recursive=True) if rows else {}


def process_documents(spark: Any, path: str):
    """Return one traceable rough row per PDF page or DOCX paragraph."""
    try:
        files = (
            spark.read.format("binaryFile")
            .option("pathGlobFilter", "*.{docx,pdf,DOCX,PDF}")
            .option("recursiveFileLookup", "true")
            .load(path)
        )
    except AnalysisException:
        return None
    except Exception:
        return None

    if files.rdd.isEmpty():
        return None
    descriptor = _descriptor(spark, path.rsplit("/", 1)[0])
    descriptor_json = json.dumps(descriptor, ensure_ascii=False, sort_keys=True)
    descriptor_schema = StructType(
        [
            StructField("input_id", StringType(), True),
            StructField("tenant_id", StringType(), True),
            StructField("source_version", StringType(), True),
            StructField("acl_digest", StringType(), True),
            StructField("acl_json", StringType(), True),
            StructField("trust_label", StringType(), True),
        ]
    )
    source = descriptor.get("source", {})
    descriptor_row = {
        "input_id": descriptor.get("input_id", "legacy"),
        "tenant_id": descriptor.get("tenant_id", "legacy"),
        "source_version": source.get("version", "sha256:unknown"),
        "acl_digest": descriptor.get("acl_digest", "unknown"),
        "acl_json": json.dumps(descriptor.get("acl", []), ensure_ascii=False, sort_keys=True),
        "trust_label": descriptor.get("trust_label", "untrusted_external"),
    }
    # Keep the descriptor materialized as literals.  ``descriptor_json`` is
    # deliberately unused after validation to make the closure explicit to Spark.
    del descriptor_json, descriptor_schema
    parse = _parse_udf(col("file_name"), col("content"))
    return (
        files.withColumn("file_name", element_at(split(col("path"), "/"), -1))
        .withColumn("parsed_records", explode(parse))
        .select(
            col("parsed_records.text").alias("text"),
            lit("documents").alias("source_name"),
            col("path").alias("source_uri"),
            col("modificationTime").cast(StringType()).alias("source_modified_at"),
            col("length").cast("long").alias("source_size"),
            sha2(col("content"), 256).alias("content_sha256"),
            lit(descriptor_row["input_id"]).alias("input_id"),
            lit(descriptor_row["tenant_id"]).alias("tenant_id"),
            lit(descriptor_row["source_version"]).alias("source_version"),
            lit(descriptor_row["acl_digest"]).alias("acl_digest"),
            lit(descriptor_row["acl_json"]).alias("acl_json"),
            lit(descriptor_row["trust_label"]).alias("trust_label"),
            col("parsed_records.page").alias("page"),
            col("parsed_records.paragraph").alias("paragraph"),
            col("parsed_records.decision").alias("decision"),
            col("parsed_records.reason_codes").alias("reason_codes"),
        )
        .filter(col("text") != "")
    )
