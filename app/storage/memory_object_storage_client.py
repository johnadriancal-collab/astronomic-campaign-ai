"""In-memory ObjectStorageClient for tests -- never touches a real
provider. Mirrors the ABC+Memory+SQLite triple pattern already used
throughout app/repositories/, applied here to a client rather than a
store."""

from app.storage.object_storage_client import ObjectStorageClient


class MemoryObjectStorageClient(ObjectStorageClient):
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        # Every key ever deleted, in call order -- lets a test assert
        # exactly which object(s) were cleaned up and in what order,
        # without needing to inspect `objects` (which only reflects
        # current state, not history).
        self.deleted_keys: list[str] = []
        # Test-only failure injection -- lets a test simulate a storage
        # outage at an exact point in ProfilePhotoService's replacement
        # transaction without needing a separate mock/fake class per
        # scenario. Both default to never failing.
        self.fail_put: bool = False
        self.fail_delete_keys: set[str] = set()

    async def put_object(self, key: str, data: bytes, content_type: str) -> None:
        if self.fail_put:
            raise ConnectionError("simulated object-storage put failure")
        self.objects[key] = data
        self.content_types[key] = content_type

    async def delete_object(self, key: str) -> None:
        if key in self.fail_delete_keys:
            raise ConnectionError("simulated object-storage delete failure")
        self.objects.pop(key, None)
        self.content_types.pop(key, None)
        self.deleted_keys.append(key)
