"""Domain event value objects used by repository contracts."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


@dataclass(frozen=True, slots=True)
class IncidentEventData:
    """A normalized incident event with an optional external idempotency key."""

    tenant_id: UUID
    incident_id: UUID
    event_type: str
    correlation_id: str
    payload: dict[str, object] = field(default_factory=dict)
    source: str = "internal"
    idempotency_key: str | None = None
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.source != "internal" and not self.idempotency_key:
            raise ValueError("External incident events require an idempotency key")
        if not _CORRELATION_ID.fullmatch(self.correlation_id):
            raise ValueError("Incident events require a valid correlation ID")
