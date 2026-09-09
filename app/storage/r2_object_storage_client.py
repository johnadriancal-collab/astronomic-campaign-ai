"""
Cloudflare R2 ObjectStorageClient -- R2 exposes an S3-compatible API, so
this is a thin boto3 wrapper, NOT R2-specific code; swapping to real AWS
S3 (or any other S3-compatible provider) later is an endpoint/credential
change here, not a rewrite (see ObjectStorageClient's own docstring).

`boto3` has no native async support, so every real network call is
pushed to a thread via `asyncio.to_thread` -- this keeps the FastAPI event
loop from blocking on synchronous I/O without adding a second dependency
(e.g. aioboto3) for what is, per Contact, a single small (~tens of KB)
upload/delete.

The underlying boto3 client is constructed LAZILY (first real call, not
`__init__`) and only from fully-configured settings -- this lets the app
start up and this module even be imported in an environment with no R2
credentials configured at all (Stage 1's current state); only an actual
attempt to upload/delete a photo fails, with a clear error, never app
startup itself.
"""

import asyncio
from typing import Any

from app.config import settings
from app.storage.object_storage_client import ObjectStorageClient


class R2NotConfiguredError(Exception):
    """Raised on first real use if any required R2 setting is missing --
    never at import time or app startup."""


class R2ObjectStorageClient(ObjectStorageClient):
    def __init__(
        self,
        bucket_name: str | None = None,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ):
        self.bucket_name = bucket_name or settings.r2_bucket_name
        self.endpoint_url = endpoint_url or settings.r2_endpoint_url
        self.access_key_id = access_key_id or settings.r2_access_key_id
        self.secret_access_key = secret_access_key or settings.r2_secret_access_key
        self._client: Any = None

    def _require_configured(self) -> None:
        missing = [
            name
            for name, value in (
                ("r2_bucket_name", self.bucket_name),
                ("r2_endpoint_url", self.endpoint_url),
                ("r2_access_key_id", self.access_key_id),
                ("r2_secret_access_key", self.secret_access_key),
            )
            if not value
        ]
        if missing:
            raise R2NotConfiguredError(
                f"Cloudflare R2 is not configured -- missing: {', '.join(missing)}. "
                "This is expected until the production-infrastructure stage provisions "
                "a real R2 bucket and credentials; no credential values are included here."
            )

    def _get_client(self):
        self._require_configured()
        if self._client is None:
            import boto3  # deferred -- never imported/constructed until actually needed

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name="auto",  # R2's own convention -- it has no real regions
            )
        return self._client

    async def put_object(self, key: str, data: bytes, content_type: str) -> None:
        client = self._get_client()
        await asyncio.to_thread(client.put_object, Bucket=self.bucket_name, Key=key, Body=data, ContentType=content_type)

    async def delete_object(self, key: str) -> None:
        client = self._get_client()
        # S3's DeleteObject is itself idempotent -- deleting an
        # already-absent key succeeds (no NoSuchKey error), satisfying
        # ObjectStorageClient's "delete never raises on an absent key"
        # contract for free, with no special-casing needed here.
        await asyncio.to_thread(client.delete_object, Bucket=self.bucket_name, Key=key)
