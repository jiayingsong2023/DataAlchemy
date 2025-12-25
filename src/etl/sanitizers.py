import re
from pyspark.sql.functions import udf
from pyspark.sql.types import StringType
from config import PATTERNS, TOKENS

def sanitize_text(text):
    """Remove sensitive information using regex patterns from config."""
    if not text:
        return ""
    
    for key, pattern in PATTERNS.items():
        replacement = TOKENS.get(key, "[REDACTED]")
        text = re.sub(pattern, replacement, text)
    
    return text

sanitize_udf = udf(sanitize_text, StringType())

