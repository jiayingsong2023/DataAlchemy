from typing import Any, Dict, List

from .base import Chunker


class RecursiveChunker(Chunker):
    """
    A simplified version of RecursiveCharacterTextSplitter.
    Splits text by trying a list of separators in order.
    """

    def __init__(self, chunk_size: int = 800, chunk_overlap: int = 100):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        # Standard separators from macro to micro
        self.separators = ["\n\n", "\n", "。 ", ". ", " ", ""]

    def split(self, text: str, metadata: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        if not text:
            return []

        chunks = self._split_text(text, self.separators)

        base_meta = metadata or {}
        return [
            {"text": chunk, "metadata": {**base_meta, "chunk_type": "recursive_character"}}
            for chunk in chunks
        ]

    def _split_text(self, text: str, separators: List[str]) -> List[str]:
        final_chunks = []

        # Get the current separator
        separator = separators[-1]
        new_separators = []
        for i, s in enumerate(separators):
            if s in text:
                separator = s
                new_separators = separators[i + 1 :]
                break

        # Split by the current separator
        if separator:
            splits = text.split(separator)
        else:
            splits = list(text)

        # Merge splits into chunks
        current_doc = []
        total = 0

        for s in splits:
            if total + len(s) + (len(separator) if current_doc else 0) <= self.chunk_size:
                current_doc.append(s)
                total += len(s) + (len(separator) if current_doc else 0)
            else:
                if current_doc:
                    doc_content = separator.join(current_doc)
                    final_chunks.append(doc_content)

                    # Overlap logic
                    overlap_doc = []
                    overlap_total = 0
                    for prev_s in reversed(current_doc):
                        if (
                            overlap_total + len(prev_s) + (len(separator) if overlap_doc else 0)
                            <= self.chunk_overlap
                        ):
                            overlap_doc.insert(0, prev_s)
                            overlap_total += len(prev_s) + (len(separator) if overlap_doc else 0)
                        else:
                            break
                    current_doc = overlap_doc
                    total = overlap_total

                # If a single split is still too large, go deeper
                if len(s) > self.chunk_size:
                    if new_separators:
                        final_chunks.extend(self._split_text(s, new_separators))
                    else:
                        final_chunks.append(s[: self.chunk_size])  # Hard cut if no separators left
                else:
                    current_doc.append(s)
                    total += len(s) + (len(separator) if current_doc else 0)

        if current_doc:
            final_chunks.append(separator.join(current_doc))

        return final_chunks
