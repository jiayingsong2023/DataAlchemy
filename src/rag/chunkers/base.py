from abc import ABC, abstractmethod
from typing import Any, Dict, List


class Chunker(ABC):
    """Base class for all chunking strategies."""

    @abstractmethod
    def split(self, text: str, metadata: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """
        Split text into chunks.
        Returns a list of dicts: [{"text": chunk_text, "metadata": {}}]
        """
        pass
