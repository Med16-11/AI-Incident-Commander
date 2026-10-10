"""Incident state machine and SQLAlchemy repository contract tests."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from opsmind_models.domain.events import IncidentEventData
from opsmind_models.domain.incidents import (
    ConcurrentIncidentUpdate,
    Incident,
    IncidentState,
    InvalidIncidentTransition,
)
from opsmind_storage.models import (
    IncidentEventRow,
    IncidentEvidenceRow,
    IncidentHypothesisRow,
    IncidentRow,
    ServiceRow,
)
from opsmind_storage.repositories import (
    IdempotencyConflict,
    IncidentNotFound,
    SqlAlchemyIncidentRepository,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def make_incident(tenant_id: UUID | None = None) -> Incident:
    """Create a deterministic valid incident fixture."""
    return Incident(tenant_id=tenant_id or uuid4(), title="API latency elevated")


def incident_in_state(state: IncidentState) -> Incident:
    """Build an incident through valid transitions to the requested state."""
    paths = {
        IncidentState.DETECTED: (),
        IncidentState.TRIAGING: (IncidentState.TRIAGING,),
        IncidentState.INVESTIGATING: (IncidentState.TRIAGING, IncidentState.INVESTIGATING),
        IncidentState.IDENTIFIED: (
            IncidentState.TRIAGING,
            IncidentState.INVESTIGATING,
            IncidentState.IDENTIFIED,
        ),
        IncidentState.MITIGATING: (
            IncidentState.TRIAGING,
            IncidentState.INVESTIGATING,
            IncidentState.IDENTIFIED,
            IncidentState.MITIGATING,
        ),
        IncidentState.RESOLVED: (
            IncidentState.TRIAGING,
            IncidentState.INVESTIGATING,
            IncidentState.IDENTIFIED,
            IncidentState.MITIGATING,
            IncidentState.RESOLVED,
        ),
        IncidentState.CLOSED: (
            IncidentState.TRIAGING,
            IncidentState.INVESTIGATING,
            IncidentState.IDENTIFIED,
            IncidentState.MITIGATING,
            IncidentState.RESOLVED,
            IncidentState.CLOSED,
        ),
    }
    incident = make_incident()
    for next_state in paths[state]:
        incident = incident.transition_to(next_state)
    return incident


def test_valid_incident_state_transitions() -> None:
    incident = make_incident()
    expected = (
        IncidentState.TRIAGING,
        IncidentState.INVESTIGATING,
        IncidentState.IDENTIFIED,
        IncidentState.MITIGATING,
        IncidentState.RESOLVED,
        IncidentState.CLOSED,
    )

    for version, state in enumerate(expected, start=2):
        incident = incident.transition_to(state)
        assert incident.state is state
        assert incident.version == version


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IncidentState.DETECTED, IncidentState.INVESTIGATING),
        (IncidentState.TRIAGING, IncidentState.RESOLVED),
        (IncidentState.INVESTIGATING, IncidentState.CLOSED),
        (IncidentState.CLOSED, IncidentState.TRIAGING),
    ],
)
def test_invalid_transitions_are_rejected(
    current: IncidentState,
    target: IncidentState,
) -> None:
    with pytest.raises(InvalidIncidentTransition):
        incident_in_state(current).transition_to(target)


def test_identified_and_mitigating_incidents_can_return_to_investigation() -> None:
    identified = make_incident().transition_to(IncidentState.TRIAGING)
    identified = identified.transition_to(IncidentState.INVESTIGATING)
    identified = identified.transition_to(IncidentState.IDENTIFIED)
    assert (
        identified.transition_to(IncidentState.INVESTIGATING).state is IncidentState.INVESTIGATING
    )

    mitigating = incident_in_state(IncidentState.MITIGATING)
    assert (
        mitigating.transition_to(IncidentState.INVESTIGATING).state is IncidentState.INVESTIGATING
    )

    resolved = incident_in_state(IncidentState.RESOLVED)
    assert resolved.transition_to(IncidentState.INVESTIGATING).state is IncidentState.INVESTIGATING


def test_repository_persists_state_and_version(db_session: Session) -> None:
    incident = make_incident()
    repository = SqlAlchemyIncidentRepository(db_session)
    repository.add(incident)

    updated = repository.transition(
        incident.tenant_id,
        incident.id,
        IncidentState.TRIAGING,
        expected_version=1,
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert updated.state is IncidentState.TRIAGING
    assert updated.version == 2
    assert repository.get(incident.tenant_id, incident.id) == updated


def test_repository_rejects_invalid_transition(db_session: Session) -> None:
    incident = make_incident()
    repository = SqlAlchemyIncidentRepository(db_session)
    repository.add(incident)

    with pytest.raises(InvalidIncidentTransition):
        repository.transition(
            incident.tenant_id,
            incident.id,
            IncidentState.RESOLVED,
            expected_version=1,
        )

    stored = repository.get(incident.tenant_id, incident.id)
    assert stored is not None
    assert stored.state is IncidentState.DETECTED


def test_reopened_incident_clears_current_resolved_timestamp(db_session: Session) -> None:
    incident = make_incident()
    repository = SqlAlchemyIncidentRepository(db_session)
    repository.add(incident)
    current = incident
    for state in (
        IncidentState.TRIAGING,
        IncidentState.INVESTIGATING,
        IncidentState.IDENTIFIED,
        IncidentState.MITIGATING,
        IncidentState.RESOLVED,
    ):
        current = repository.transition(
            incident.tenant_id,
            incident.id,
            state,
            expected_version=current.version,
        )

    stored_row = db_session.get(IncidentRow, incident.id)
    assert stored_row is not None and stored_row.resolved_at is not None
    repository.transition(
        incident.tenant_id,
        incident.id,
        IncidentState.INVESTIGATING,
        expected_version=current.version,
    )

    assert stored_row.resolved_at is None


def test_event_replay_is_idempotent_and_does_not_duplicate_rows(db_session: Session) -> None:
    incident = make_incident()
    repository = SqlAlchemyIncidentRepository(db_session)
    repository.add(incident)
    event = IncidentEventData(
        tenant_id=incident.tenant_id,
        incident_id=incident.id,
        event_type="alert.received",
        source="prometheus",
        idempotency_key="delivery-42",
        correlation_id="request-42",
        payload={"alert": "high-latency"},
    )

    first, first_created = repository.add_event_once(event)
    replay, replay_created = repository.add_event_once(
        IncidentEventData(
            tenant_id=incident.tenant_id,
            incident_id=incident.id,
            event_type="alert.received",
            source="prometheus",
            idempotency_key="delivery-42",
            correlation_id="request-retry",
            payload={"alert": "high-latency"},
        )
    )

    assert first_created is True
    assert replay_created is False
    assert replay.id == first.id
    assert replay.payload == {"alert": "high-latency"}
    assert db_session.scalar(select(func.count()).select_from(IncidentEventRow)) == 1


def test_idempotency_key_reuse_with_different_data_is_rejected(db_session: Session) -> None:
    incident = make_incident()
    repository = SqlAlchemyIncidentRepository(db_session)
    repository.add(incident)
    original = IncidentEventData(
        tenant_id=incident.tenant_id,
        incident_id=incident.id,
        event_type="alert.received",
        source="prometheus:integration-1",
        idempotency_key="delivery-42",
        correlation_id="request-42",
        payload={"alert": "high-latency"},
    )
    repository.add_event_once(original)

    with pytest.raises(IdempotencyConflict):
        repository.add_event_once(
            IncidentEventData(
                tenant_id=incident.tenant_id,
                incident_id=incident.id,
                event_type="alert.received",
                source="prometheus:integration-1",
                idempotency_key="delivery-42",
                correlation_id="request-retry",
                payload={"alert": "different-event"},
            )
        )


def test_idempotency_keys_are_scoped_by_tenant_and_source(db_session: Session) -> None:
    incident = make_incident()
    other_tenant_incident = make_incident()
    repository = SqlAlchemyIncidentRepository(db_session)
    repository.add(incident)
    repository.add(other_tenant_incident)

    for tenant_id, incident_id, source in (
        (incident.tenant_id, incident.id, "prometheus"),
        (incident.tenant_id, incident.id, "slack"),
        (other_tenant_incident.tenant_id, other_tenant_incident.id, "prometheus"),
    ):
        inserted, created = repository.add_event_once(
            IncidentEventData(
                tenant_id=tenant_id,
                incident_id=incident_id,
                event_type="external.update",
                source=source,
                idempotency_key="same-delivery-id",
                correlation_id="request-1",
            )
        )
        assert created is True
        assert inserted.source == source

    assert db_session.scalar(select(func.count()).select_from(IncidentEventRow)) == 3


def test_external_events_require_an_idempotency_key() -> None:
    with pytest.raises(ValueError, match="require an idempotency key"):
        IncidentEventData(
            tenant_id=uuid4(),
            incident_id=uuid4(),
            event_type="external.update",
            source="gitlab",
            correlation_id="request-1",
        )


def test_concurrent_incident_updates_are_rejected(db_session: Session) -> None:
    incident = make_incident()
    SqlAlchemyIncidentRepository(db_session).add(incident)
    db_session.commit()

    bind = db_session.get_bind()
    session_a = Session(bind, expire_on_commit=False)
    session_b = Session(bind, expire_on_commit=False)
    repository_a = SqlAlchemyIncidentRepository(session_a)
    repository_b = SqlAlchemyIncidentRepository(session_b)
    first_read = repository_a.get(incident.tenant_id, incident.id)
    second_read = repository_b.get(incident.tenant_id, incident.id)
    assert first_read is not None and first_read.version == 1
    assert second_read is not None and second_read.version == 1
    session_b.commit()

    repository_a.transition(
        incident.tenant_id,
        incident.id,
        IncidentState.TRIAGING,
        expected_version=1,
    )
    session_a.commit()

    with pytest.raises(ConcurrentIncidentUpdate):
        repository_b.transition(
            incident.tenant_id,
            incident.id,
            IncidentState.TRIAGING,
            expected_version=1,
        )

    session_b.close()
    stored = SqlAlchemyIncidentRepository(db_session).get(incident.tenant_id, incident.id)
    assert stored is not None and stored.version == 2


def test_incidents_are_tenant_scoped(db_session: Session) -> None:
    incident = make_incident()
    repository = SqlAlchemyIncidentRepository(db_session)
    repository.add(incident)

    assert repository.get(uuid4(), incident.id) is None
    with pytest.raises(IncidentNotFound):
        repository.transition(
            uuid4(),
            incident.id,
            IncidentState.TRIAGING,
            expected_version=1,
        )


def test_database_enforces_incident_state_constraint(db_session: Session) -> None:
    incident = IncidentRow(
        tenant_id=uuid4(),
        title="Invalid state fixture",
        state="NOT_A_STATE",
        severity="SEV3",
    )
    db_session.add(incident)

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_orm_generates_uuid_identifiers(db_session: Session) -> None:
    service = ServiceRow(tenant_id=uuid4(), name="checkout", environment="production")
    db_session.add(service)
    db_session.flush()

    assert service.id is not None


def test_hypothesis_links_to_normalized_evidence_rows(db_session: Session) -> None:
    incident = make_incident()
    SqlAlchemyIncidentRepository(db_session).add(incident)
    evidence = IncidentEvidenceRow(
        tenant_id=incident.tenant_id,
        incident_id=incident.id,
        evidence_type="deployment",
        source="gitlab",
        service="checkout",
        normalized_summary="Deployment commit abc123",
        confidence=1.0,
        content={"commit": "abc123"},
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    hypothesis = IncidentHypothesisRow(
        tenant_id=incident.tenant_id,
        incident_id=incident.id,
        summary="A recent deployment introduced the regression",
        confidence=0.8,
        evidence=[evidence],
    )
    db_session.add(hypothesis)
    db_session.flush()

    persisted = db_session.get(IncidentHypothesisRow, hypothesis.id)
    assert persisted is not None
    assert [item.id for item in persisted.evidence] == [evidence.id]
