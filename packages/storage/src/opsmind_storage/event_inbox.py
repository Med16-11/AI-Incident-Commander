"""SQLAlchemy transactional inbox for Redis Streams consumers."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from opsmind_storage.models import ProcessedEventRow


class SqlAlchemyEventUnitOfWork:
    """Inbox operations bound to the same SQL transaction as handler writes."""

    def __init__(self, session: Session, consumer_group: str) -> None:
        self.session = session
        self.consumer_group = consumer_group

    @property
    def database_session(self) -> Session:
        """Transaction-scoped SQLAlchemy session for repository adapters."""
        return self.session

    def is_processed(self, event_id: UUID) -> bool:
        statement = select(ProcessedEventRow.event_id).where(
            ProcessedEventRow.consumer_group == self.consumer_group,
            ProcessedEventRow.event_id == event_id,
        )
        return self.session.scalar(statement) is not None

    def mark_processed(self, event_id: UUID) -> None:
        self.session.add(ProcessedEventRow(consumer_group=self.consumer_group, event_id=event_id))


class SqlAlchemyEventUnitOfWorkFactory:
    """Creates and commits a fresh transaction for each message."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @contextmanager
    def __call__(self, consumer_group: str) -> Iterator[SqlAlchemyEventUnitOfWork]:
        with self._session_factory() as session, session.begin():
            yield SqlAlchemyEventUnitOfWork(session, consumer_group)
