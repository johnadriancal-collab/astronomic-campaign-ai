"""
The one interface every object-storage-backed feature (Profile Photos
today, anything else later) must call through -- see
app/services/profile_photo_service.py's module docstring for why business
logic never calls boto3 (or any provider SDK) directly. `ObjectStorageClient`
is deliberately provider-agnostic: swapping Cloudflare R2 for real AWS S3,
or anything else S3-API-compatible, is an implementation swap behind this
same two-method contract, never a change to any caller.
"""

from abc import ABC, abstractmethod


class ObjectStorageClient(ABC):
    @abstractmethod
    async def put_object(self, key: str, data: bytes, content_type: str) -> None:
        """Uploads `data` under `key`, creating or overwriting it. Must
        raise on failure -- callers rely on an exception (never a falsy
        return value) to detect an unsuccessful upload."""

    @abstractmethod
    async def delete_object(self, key: str) -> None:
        """Deletes the object at `key`. Deleting an already-absent key
        must NOT raise -- callers (see ProfilePhotoService's replacement
        transaction) treat delete as a best-effort cleanup step, never a
        condition to roll back an already-successful Contact update over."""
