import io
import os
import docx
from pypdf import PdfReader
from pyspark.sql.functions import col, lit, concat_ws, udf, element_at, split
from pyspark.sql.types import StringType
from pyspark.sql.utils import AnalysisException
from cleaners.base import normalize_whitespace_udf
from sanitizers import sanitize_udf

def parse_docx(binary_content):
    try:
        doc = docx.Document(io.BytesIO(binary_content))
        return "\n".join([para.text for para in doc.paragraphs])
    except: return ""

def parse_pdf(binary_content):
    try:
        reader = PdfReader(io.BytesIO(binary_content))
        return "\n".join([page.extract_text() or "" for page in reader.pages])
    except: return ""

# Register parsing functions as UDFs
parse_docx_udf = udf(parse_docx, StringType())
parse_pdf_udf = udf(parse_pdf, StringType())

def process_documents(spark, path):
    """Process .docx and .pdf files using Spark native binaryFile reader."""
    try:
        try:
            # Read all files in the directory as binary
            # This supports S3 paths
            df = spark.read.format("binaryFile") \
                .option("pathGlobFilter", "*.{docx,pdf,DOCX,PDF}") \
                .option("recursiveFileLookup", "true") \
                .load(path)
        except AnalysisException:
            print(f"  [WARN] Path not found or empty: {path}")
            return None
        except Exception as e:
            print(f"  [WARN] Error reading path {path}: {e}")
            return None
            
        if df.rdd.isEmpty():
            return None
            
        # df schema: [path, modificationTime, length, content]
        
        # Extract filename from path
        # path is like s3a://bucket/dir/file.docx
        df = df.withColumn("file_name", element_at(split(col("path"), "/"), -1))
        
        # Determine source type and parse content
        # We can use a single UDF that dispatches based on extension, or separate columns
        # Let's try a conditional approach
        
        df = df.withColumn("source_type", 
            udf(lambda f: "DOCX" if f.lower().endswith(".docx") else ("PDF" if f.lower().endswith(".pdf") else "UNKNOWN"), StringType())(col("file_name"))
        )
        
        # Parse content based on type
        # Note: We apply both UDFs but only use one result. This is a bit inefficient but simple.
        # A better way is a single UDF that takes content and filename.
        
        @udf(returnType=StringType())
        def parse_content_udf(filename, content):
            if filename.lower().endswith(".docx"):
                return parse_docx(content)
            elif filename.lower().endswith(".pdf"):
                return parse_pdf(content)
            return ""

        df = df.withColumn("raw_content", parse_content_udf(col("file_name"), col("content")))
        
        # Filter out empty content
        df = df.filter(col("raw_content") != "")
        
        processed_df = df.select(
            concat_ws(
                "\n\n",
                lit("### Document Source"),
                concat_ws(": ", lit("File"), col("file_name")),
                concat_ws(": ", lit("Type"), col("source_type")),
                concat_ws(": ", lit("Content"), col("raw_content"))
            ).alias("raw_text")
        )
        
        return processed_df.select(
            sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text")
        )
    except Exception as e:
        print(f"Error processing documents: {e}")
        return None

