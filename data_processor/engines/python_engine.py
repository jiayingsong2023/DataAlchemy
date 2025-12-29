"""
Pure Python Data Processing Engine
- Zero external dependencies (no Spark, no Hadoop)
- Optimized for: Windows, macOS, single-machine demo
- Data scale: < 1GB (thousands to tens of thousands of records)
"""
import os
import json
import io
import sys

# Add parent directory to path to allow importing from cleaners and sanitizers
# when run from within the engines subdirectory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import PDF/DOCX parsers
try:
    import docx
    from pypdf import PdfReader
    HAS_DOC_PARSERS = True
except ImportError:
    HAS_DOC_PARSERS = False

# Try to import from main project config, fallback to local defaults if standalone
try:
    from config import (
        GIT_PR_PATH, JIRA_PATH, CONFLUENCE_PATH, DOCUMENTS_PATH,
        PATTERNS, TOKENS, FEEDBACK_DATA_DIR
    )
except ImportError:
    # Minimal defaults for standalone execution
    GIT_PR_PATH = "data/raw/git_pr"
    JIRA_PATH = "data/raw/jira"
    CONFLUENCE_PATH = "data/raw/confluence"
    DOCUMENTS_PATH = "data/raw/documents"
    FEEDBACK_DATA_DIR = "data/feedback"
    PATTERNS = {}
    TOKENS = {}

from cleaners.base import clean_html, normalize_whitespace
from sanitizers import sanitize_text


class PythonEngine:
    """Pure Python processing engine for small-scale data."""
    
    def __init__(self):
        self.engine_name = "PythonEngine"
        print(f"[{self.engine_name}] Initialized (No Spark/Hadoop)")
    
    def _process_text(self, text: str) -> str:
        """Full cleaning pipeline."""
        return sanitize_text(normalize_whitespace(clean_html(text)))
    
    def _chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 50) -> list:
        """Simple sliding window chunking."""
        if not text:
            return []
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunks.append(text[start:end])
            if end >= len(text):
                break
            start += (chunk_size - overlap)
        return chunks

    # --- Data Source Processors ---
    def _process_json_files(self, path: str, source_label: str, 
                            title_key: str, content_key: str) -> dict:
        """Generic processor for JSON/JSONL files."""
        sft_results = []
        rag_results = []
        if not os.path.exists(path):
            return {"sft": sft_results, "rag": rag_results}
        
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
                        
                        # Process for SFT
                        cleaned_content = self._process_text(content)
                        sft_text = f"### {source_label}\nTitle: {title}\nContent: {cleaned_content}"
                        sft_results.append({"text": sft_text})
                        
                        # Process for RAG
                        chunks = self._chunk_text(cleaned_content)
                        for i, chunk in enumerate(chunks):
                            rag_results.append({
                                "text": chunk,
                                "metadata": {
                                    "source": source_label,
                                    "title": title,
                                    "file": f,
                                    "chunk_id": i
                                }
                            })
            except Exception as e:
                print(f"  [WARN] Error processing {f}: {e}")
        return {"sft": sft_results, "rag": rag_results}
    
    def _process_documents(self, path: str) -> dict:
        """Process PDF and DOCX files."""
        sft_results = []
        rag_results = []
        if not os.path.exists(path):
            return {"sft": sft_results, "rag": rag_results}
        if not HAS_DOC_PARSERS:
            print("  [WARN] docx/pypdf not installed, skipping documents")
            return {"sft": sft_results, "rag": rag_results}

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
                    cleaned_content = self._process_text(content)
                    sft_text = f"### Document Source\nFile: {f}\nType: {source_type}\nContent: {cleaned_content}"
                    sft_results.append({"text": sft_text})
                    
                    # RAG Chunks
                    chunks = self._chunk_text(cleaned_content)
                    for i, chunk in enumerate(chunks):
                        rag_results.append({
                            "text": chunk,
                            "metadata": {
                                "source": "Document",
                                "file": f,
                                "type": source_type,
                                "chunk_id": i
                            }
                        })
            except Exception as e:
                print(f"  [WARN] Error processing {f}: {e}")
        return {"sft": sft_results, "rag": rag_results}

    def _process_feedback(self, path: str) -> dict:
        """Process user feedback JSON files."""
        sft_results = []
        rag_results = []
        if not os.path.exists(path):
            return {"sft": sft_results, "rag": rag_results}
            
        for f in os.listdir(path):
            if not f.endswith('.json'):
                continue
            try:
                with open(os.path.join(path, f), 'r', encoding='utf-8') as file:
                    data = json.load(file)
                    query = data.get("query", "")
                    answer = data.get("answer", "")
                    feedback = data.get("feedback", "good")
                    
                    # Only use "good" feedback for training
                    if feedback == "good":
                        sft_text = f"### User Feedback\nQuestion: {query}\nAnswer: {answer}"
                        sft_results.append({"text": sft_text})
                        
                        # Also add to RAG
                        rag_results.append({
                            "text": f"Question: {query}\nAnswer: {answer}",
                            "metadata": {
                                "source": "User Feedback",
                                "file": f
                            }
                        })
            except Exception as e:
                print(f"  [WARN] Error processing feedback {f}: {e}")
        return {"sft": sft_results, "rag": rag_results}

    # --- Main Processing Method ---
    def process_all(self) -> dict:
        """Process all data sources and return combined results."""
        all_sft = []
        all_rag = []
        
        # 1. Git PRs
        print("\n[1/5] Processing Git PRs...")
        res = self._process_json_files(GIT_PR_PATH, "Git PR", "title", "description")
        print(f"  Found {len(res['sft'])} records.")
        all_sft.extend(res['sft'])
        all_rag.extend(res['rag'])
        
        # 2. Jira
        print("[2/5] Processing Jira Issues...")
        res = self._process_json_files(JIRA_PATH, "Jira Issue", "summary", "description")
        print(f"  Found {len(res['sft'])} records.")
        all_sft.extend(res['sft'])
        all_rag.extend(res['rag'])
        
        # 3. Confluence
        print("[3/5] Processing Confluence Pages...")
        res = self._process_json_files(CONFLUENCE_PATH, "Confluence Page", "title", "body")
        print(f"  Found {len(res['sft'])} records.")
        all_sft.extend(res['sft'])
        all_rag.extend(res['rag'])
        
        # 4. Documents
        print("[4/5] Processing Binary Documents (PDF/DOCX)...")
        res = self._process_documents(DOCUMENTS_PATH)
        print(f"  Found {len(res['sft'])} records.")
        all_sft.extend(res['sft'])
        all_rag.extend(res['rag'])

        # 5. User Feedback
        print("[5/5] Processing User Feedback...")
        res = self._process_feedback(FEEDBACK_DATA_DIR)
        print(f"  Found {len(res['sft'])} good records.")
        all_sft.extend(res['sft'])
        all_rag.extend(res['rag'])
        
        return {"sft": all_sft, "rag": all_rag}
    
    def stop(self):
        """No-op for compatibility with Spark engine interface."""
        pass
