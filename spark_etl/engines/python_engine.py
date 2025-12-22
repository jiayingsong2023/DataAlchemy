"""
Pure Python Data Processing Engine
- Zero external dependencies (no Spark, no Hadoop)
- Optimized for: Windows, macOS, single-machine demo
- Data scale: < 1GB (thousands to tens of thousands of records)
"""
import os
import json
import re
from bs4 import BeautifulSoup

# Import PDF/DOCX parsers
try:
    import docx
    from pypdf import PdfReader
    HAS_DOC_PARSERS = True
except ImportError:
    HAS_DOC_PARSERS = False

from spark_etl.config import (
    GIT_PR_PATH, JIRA_PATH, CONFLUENCE_PATH, DOCUMENTS_PATH,
    PATTERNS, TOKENS
)


class PythonEngine:
    """Pure Python processing engine for small-scale data."""
    
    def __init__(self):
        self.engine_name = "PythonEngine"
        print(f"[{self.engine_name}] Initialized (No Spark/Hadoop)")
    
    # --- Cleaning Functions ---
    def _clean_html(self, text: str) -> str:
        if not text:
            return ""
        try:
            return BeautifulSoup(text, "html.parser").get_text(separator=" ").strip()
        except Exception:
            return re.sub(r'<[^>]+>', '', text).strip()
    
    def _normalize_whitespace(self, text: str) -> str:
        if not text:
            return ""
        return re.sub(r'\s+', ' ', text).strip()
    
    def _sanitize_text(self, text: str) -> str:
        if not text:
            return ""
        for key, pattern in PATTERNS.items():
            replacement = TOKENS.get(key, "[REDACTED]")
            text = re.sub(pattern, replacement, text)
        return text
    
    def _process_text(self, text: str) -> str:
        """Full cleaning pipeline."""
        return self._sanitize_text(self._normalize_whitespace(self._clean_html(text)))
    
    # --- Data Source Processors ---
    def _process_json_files(self, path: str, source_label: str, 
                            title_key: str, content_key: str) -> list:
        """Generic processor for JSON/JSONL files."""
        results = []
        if not os.path.exists(path):
            return results
        
        for f in os.listdir(path):
            if not f.endswith('.json'):
                continue
            try:
                with open(os.path.join(path, f), 'r', encoding='utf-8') as file:
                    for line in file:
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        title = data.get(title_key, "")
                        content = data.get(content_key, "")
                        text = f"### {source_label}\nTitle: {title}\nContent: {self._process_text(content)}"
                        results.append({"text": text})
            except Exception as e:
                print(f"  [WARN] Error processing {f}: {e}")
        return results
    
    def _process_documents(self, path: str) -> list:
        """Process PDF and DOCX files."""
        results = []
        if not os.path.exists(path):
            return results
        if not HAS_DOC_PARSERS:
            print("  [WARN] docx/pypdf not installed, skipping documents")
            return results

        for f in os.listdir(path):
            full_path = os.path.join(path, f)
            if not os.path.isfile(full_path):
                continue
            
            try:
                content = ""
                source_type = ""
                
                if f.lower().endswith(".pdf"):
                    with open(full_path, 'rb') as file:
                        reader = PdfReader(file)
                        content = "\n".join([p.extract_text() or "" for p in reader.pages])
                    source_type = "PDF"
                elif f.lower().endswith(".docx"):
                    doc = docx.Document(full_path)
                    content = "\n".join([para.text for para in doc.paragraphs])
                    source_type = "DOCX"
                else:
                    continue
                
                if content.strip():
                    text = f"### Document Source\nFile: {f}\nType: {source_type}\nContent: {self._process_text(content)}"
                    results.append({"text": text})
            except Exception as e:
                print(f"  [WARN] Error processing {f}: {e}")
        return results
    
    # --- Main Processing Method ---
    def process_all(self) -> list:
        """Process all data sources and return combined results."""
        all_results = []
        
        # 1. Git PRs
        print("\n[1/4] Processing Git PRs...")
        results = self._process_json_files(GIT_PR_PATH, "Git PR", "title", "description")
        print(f"  Found {len(results)} records.")
        all_results.extend(results)
        
        # 2. Jira
        print("[2/4] Processing Jira Issues...")
        results = self._process_json_files(JIRA_PATH, "Jira Issue", "summary", "description")
        print(f"  Found {len(results)} records.")
        all_results.extend(results)
        
        # 3. Confluence
        print("[3/4] Processing Confluence Pages...")
        results = self._process_json_files(CONFLUENCE_PATH, "Confluence Page", "title", "body")
        print(f"  Found {len(results)} records.")
        all_results.extend(results)
        
        # 4. Documents
        print("[4/4] Processing Binary Documents (PDF/DOCX)...")
        results = self._process_documents(DOCUMENTS_PATH)
        print(f"  Found {len(results)} records.")
        all_results.extend(results)
        
        return all_results
    
    def stop(self):
        """No-op for compatibility with Spark engine interface."""
        pass

