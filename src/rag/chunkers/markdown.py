import re
from typing import Any, Dict, List
from .base import Chunker

class MarkdownChunker(Chunker):
    """
    Splits markdown text based on header levels (1-4).
    Preserves header hierarchy in metadata.
    """
    def __init__(self, max_chunk_size: int = 1000, min_chunk_size: int = 100):
        self.max_chunk_size = max_chunk_size
        self.min_chunk_size = min_chunk_size

    def split(self, text: str, metadata: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        if not text:
            return []
            
        # Pattern to find headers (h1 to h4) at the start of a line
        header_pattern = r"^(#{1,4})\s+(.+)$"
        
        lines = text.split("\n")
        chunks = []
        current_chunk_lines = []
        current_headers = {}
        
        base_meta = metadata or {}
        
        for line in lines:
            header_match = re.match(header_pattern, line)
            if header_match:
                # If we have accumulated enough content, flush it as a new chunk
                if current_chunk_lines and self._calc_len(current_chunk_lines) >= self.min_chunk_size:
                    chunks.append(self._create_chunk(current_chunk_lines, base_meta, current_headers))
                    current_chunk_lines = []
                
                # Update hierarchy
                level = len(header_match.group(1))
                title = header_match.group(2).strip()
                current_headers[f"h{level}"] = title
                # Clear lower level headers to maintain correct hierarchy
                for i in range(level + 1, 5):
                    current_headers.pop(f"h{i}", None)
            
            current_chunk_lines.append(line)
            
            # Force split if exceeding max size
            if self._calc_len(current_chunk_lines) > self.max_chunk_size:
                chunks.append(self._create_chunk(current_chunk_lines, base_meta, current_headers))
                current_chunk_lines = []
                
        # Final flush
        if current_chunk_lines:
            chunk_text = "\n".join(current_chunk_lines).strip()
            if len(chunk_text) > 0:
                chunks.append(self._create_chunk(current_chunk_lines, base_meta, current_headers))
        
        return chunks

    def _calc_len(self, lines: List[str]) -> int:
        return sum(len(l) for l in lines) + len(lines)

    def _create_chunk(self, lines: List[str], base_meta: Dict, headers: Dict) -> Dict:
        return {
            "text": "\n".join(lines).strip(),
            "metadata": {**base_meta, **headers.copy(), "chunk_type": "markdown_semantic"}
        }
