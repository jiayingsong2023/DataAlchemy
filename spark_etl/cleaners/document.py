import io
import docx
from pypdf import PdfReader
from pyspark.sql.functions import col, lit
from spark_etl.cleaners.base import normalize_whitespace_udf
from spark_etl.sanitizers import sanitize_udf

def parse_docx(binary_content):
    """Extract text from docx binary content."""
    try:
        doc = docx.Document(io.BytesIO(binary_content))
        full_text = []
        for para in doc.paragraphs:
            full_text.append(para.text)
        return "\n".join(full_text)
    except Exception as e:
        print(f"Error parsing DOCX: {e}")
        return ""

def parse_pdf(binary_content):
    """Extract text from PDF binary content."""
    try:
        reader = PdfReader(io.BytesIO(binary_content))
        full_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                full_text.append(text)
        return "\n".join(full_text)
    except Exception as e:
        print(f"Error parsing PDF: {e}")
        return ""

def process_documents(spark, path):
    """
    Process .docx and .pdf files from a directory.
    Uses sparkContext.binaryFiles for parallel processing.
    """
    # Read files as (path, content) pairs
    binary_rdd = spark.sparkContext.binaryFiles(path)
    
    def extract_content(file_info):
        file_path, content = file_info
        if file_path.endswith(".docx"):
            text = parse_docx(content)
            source_type = "DOCX"
        elif file_path.endswith(".pdf"):
            text = parse_pdf(content)
            source_type = "PDF"
        else:
            text = ""
            source_type = "Unknown"
        
        file_name = file_path.split("/")[-1]
        return (file_name, source_type, text)

    # Transform RDD to DataFrame
    content_rdd = binary_rdd.map(extract_content).filter(lambda x: x[2] != "")
    
    # Create DataFrame with proper schema
    from pyspark.sql.types import StructType, StructField, StringType
    schema = StructType([
        StructField("file_name", StringType(), True),
        StructField("source_type", StringType(), True),
        StructField("raw_content", StringType(), True)
    ])
    
    df = spark.createDataFrame(content_rdd, schema)
    
    # Final formatting and cleaning
    from pyspark.sql.functions import concat_ws
    
    processed_df = df.select(
        concat_ws(
            "\n\n",
            lit("### Document Source"),
            concat_ws(": ", lit("File"), col("file_name")),
            concat_ws(": ", lit("Type"), col("source_type")),
            concat_ws(": ", lit("Content"), col("raw_content"))
        ).alias("raw_text")
    )
    
    final_df = processed_df.select(
        sanitize_udf(normalize_whitespace_udf(col("raw_text"))).alias("text")
    )
    
    return final_df

