"""
Storage abstraction for EmailMessage. Mirrors EmailSequenceStore's shape:
EmailMessageSyncService depends only on this interface.

`get_by_apollo_message_id` is the sync dedup lookup -- the sync service
uses it to decide upsert-as-update vs upsert-as-create; `apollo_message_id`
is None only for test-fixture rows, which never go through that path.
"""

from abc import ABC, abstractmethod

from app.models.email_message import EmailMessage


class EmailMessageNotFoundError(Exception):
    def __init__(self, email_message_id: str):
        self.email_message_id = email_message_id
        super().__init__(f"EmailMessage not found: {email_message_id}")


class EmailMessageStore(ABC):
    @abstractmethod
    async def create(self, message: EmailMessage) -> None:
        """Persist a newly-created message. Raises if email_message_id already exists."""

    @abstractmethod
    async def get(self, email_message_id: str) -> EmailMessage | None:
        """Returns the message, or None if it doesn't exist."""

    @abstractmethod
    async def get_by_apollo_message_id(self, apollo_message_id: str) -> EmailMessage | None:
        """The sync dedup lookup -- one EmailMessage per Apollo message id, ever."""

    @abstractmethod
    async def save(self, message: EmailMessage) -> None:
        """Persist mutations to an existing message."""

    @abstractmethod
    async def list_for_sequence(self, email_sequence_id: str) -> list[EmailMessage]:
        """Every message belonging to this sequence, across both real and fixture sources."""


class MemoryEmailMessageStore(EmailMessageStore):
    """Dictionary-backed, keyed by email_message_id -- not persistent, for tests/local dev."""

    def __init__(self):
        self._messages: dict[str, EmailMessage] = {}

    async def create(self, message: EmailMessage) -> None:
        if message.email_message_id in self._messages:
            raise ValueError(f"EmailMessage already exists: {message.email_message_id}")
        self._messages[message.email_message_id] = message

    async def get(self, email_message_id: str) -> EmailMessage | None:
        return self._messages.get(email_message_id)

    async def get_by_apollo_message_id(self, apollo_message_id: str) -> EmailMessage | None:
        for message in self._messages.values():
            if message.apollo_message_id == apollo_message_id:
                return message
        return None

    async def save(self, message: EmailMessage) -> None:
        if message.email_message_id not in self._messages:
            raise EmailMessageNotFoundError(message.email_message_id)
        self._messages[message.email_message_id] = message

    async def list_for_sequence(self, email_sequence_id: str) -> list[EmailMessage]:
        return [m for m in self._messages.values() if m.email_sequence_id == email_sequence_id]
