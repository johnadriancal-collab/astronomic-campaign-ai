"""ABC + in-memory MailboxCredentialStore. INTERNAL ONLY -- see
app/models/mailbox.py's MailboxCredential docstring. Never imported by
app/api/mailboxes.py."""

from abc import ABC, abstractmethod

from app.models.mailbox import MailboxCredential


class MailboxCredentialStore(ABC):
    @abstractmethod
    async def create(self, credential: MailboxCredential) -> None: ...

    @abstractmethod
    async def get(self, mailbox_id: str) -> MailboxCredential | None: ...

    @abstractmethod
    async def save(self, credential: MailboxCredential) -> None: ...

    @abstractmethod
    async def delete(self, mailbox_id: str) -> None: ...


class MemoryMailboxCredentialStore(MailboxCredentialStore):
    def __init__(self):
        self._rows: dict[str, MailboxCredential] = {}

    async def create(self, credential: MailboxCredential) -> None:
        self._rows[credential.mailbox_id] = credential

    async def get(self, mailbox_id: str) -> MailboxCredential | None:
        return self._rows.get(mailbox_id)

    async def save(self, credential: MailboxCredential) -> None:
        self._rows[credential.mailbox_id] = credential

    async def delete(self, mailbox_id: str) -> None:
        self._rows.pop(mailbox_id, None)
