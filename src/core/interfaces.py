from typing import Protocol, Any, List, Optional, Dict

class StorageInterface(Protocol):
    """Abstract interface for storage operations."""
    def put_object(self, s3_key: str, body: Any, content_type: str = "application/json") -> str:
        ...

    def get_object(self, s3_key: str) -> Any:
        ...

    def list_objects(self, prefix: str) -> List[str]:
        ...

class AgentInterface(Protocol):
    """Abstract interface for AI Agents."""
    async def predict_async(self, query: str, **kwargs) -> str:
        ...

    def predict(self, query: str, **kwargs) -> str:
        ...
