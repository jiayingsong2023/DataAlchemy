from typing import Any, List, Protocol


class StorageInterface(Protocol):
    """Abstract interface for storage operations."""

    def put_object(self, s3_key: str, body: Any, content_type: str = "application/json") -> str: ...

    def get_object(self, s3_key: str) -> Any: ...

    def list_objects(self, prefix: str) -> List[str]: ...
