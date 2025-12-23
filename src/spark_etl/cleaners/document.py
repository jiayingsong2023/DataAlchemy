import io
import os
import docx
from pypdf import PdfReader
from pyspark.sql.functions import col, lit, concat_ws
from spark_etl.cleaners.base import normalize_whitespace_udf
from spark_etl.sanitizers import sanitize_udf

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

def process_documents(spark, path):
    """Process .docx and .pdf files using Python reading to bypass Hadoop issues."""
    try:
        data = []
        for f in os.listdir(path):
            full_path = os.path.join(path, f)
            if not os.path.isfile(full_path): continue
            
            with open(full_path, 'rb') as file:
                content = file.read()
            
            if f.lower().endswith(".docx"):
                text = parse_docx(content)
                source_type = "DOCX"
            elif f.lower().endswith(".pdf"):
                text = parse_pdf(content)
                source_type = "PDF"
            else: continue
            
            if text.strip():
                data.append({
                    "file_name": f,
                    "source_type": source_type,
                    "raw_content": text
                })
        
        if not data: return None

        # Convert to Spark DataFrame
        df = spark.createDataFrame(data)
        
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
