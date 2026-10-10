"""Persistence and handler ports used by the stream consumer."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

from opsmind_models.events import EventEnvelope, EventPayload


class EventUnitOfWork(Protocol):
    """Inbox and application writes that commit atomically for one event."""

    def is_processed(self, event_id: UUID) -> bool: ...

    def mark_processed(self, event_id: UUID) -> None: ...

    @property
    def session(self) -> object:
        """Underlying transaction handle for transaction-scoped repositories."""
        ...


class EventUnitOfWorkFactory(Protocol):
    """Creates a transaction-scoped inbox/application unit of work."""

    def __call__(self, consumer_group: str) -> AbstractContextManager[EventUnitOfWork]: ...


class EventHandler(Protocol):
    """Application callback; its writes must use the supplied unit of work."""

    def __call__(self, event: EventEnvelope[EventPayload], uow: EventUnitOfWork) -> None: ...


Sleep = Callable[[float], None]
