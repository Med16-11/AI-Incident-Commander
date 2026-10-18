"""Health endpoint and request-correlation contract tests."""

from collections.abc import Callable, Iterator
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from opsmind_api.auth import Principal
from opsmind_api.main import create_app
from opsmind_common.config import Settings
from opsmind_common.correlation import CorrelationIdMiddleware
from opsmind_detection_engine.main import create_app as create_detection_app
from opsmind_incident_engine.main import create_app as create_incident_app
from opsmind_notification_service.main import app as notification_app
from opsmind_remediation_engine.main import create_app as create_remediation_app
from opsmind_worker.main import app as worker_app
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.types import ASGIApp

_api_health_engine = create_engine("sqlite://", poolclass=StaticPool)
_api_health_sessions = sessionmaker(bind=_api_health_engine, class_=Session)
api_app = create_app(session_factory=_api_health_sessions)
_incident_health_engine = create_engine("sqlite://", poolclass=StaticPool)
_incident_health_sessions = sessionmaker(bind=_incident_health_engine, class_=Session)
incident_app = create_incident_app(session_factory=_incident_health_sessions)
_remediation_health_engine = create_engine("sqlite://", poolclass=StaticPool)
_remediation_health_sessions = sessionmaker(bind=_remediation_health_engine, class_=Session)
remediation_app = create_remediation_app(session_factory=_remediation_health_sessions)
detection_app = create_detection_app(
    Settings(event_ingestion_token=SecretStr("unit-test-token-with-sufficient-length"))
)

SERVICES: tuple[tuple[str, ASGIApp], ...] = (
    ("api", api_app),
    ("incident-engine", incident_app),
    ("detection-engine", detection_app),
    ("remediation-engine", remediation_app),
    ("notification-service", notification_app),
    ("worker", worker_app),
)


@pytest.fixture(params=SERVICES, ids=[name for name, _ in SERVICES])
def service(request: pytest.FixtureRequest) -> Iterator[tuple[str, TestClient]]:
    name, app = request.param
    with TestClient(app) as client:
        yield name, client


def test_health_endpoints_return_service_and_correlation(service: tuple[str, TestClient]) -> None:
    name, client = service
    response = client.get("/health/ready", headers={"X-Correlation-ID": "test-correlation-123"})

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "service": name}
    assert response.headers["X-Correlation-ID"] == "test-correlation-123"


def test_correlation_id_is_generated_when_missing(service: tuple[str, TestClient]) -> None:
    _, client = service
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.headers.get("X-Correlation-ID")


def test_invalid_correlation_id_is_replaced(service: tuple[str, TestClient]) -> None:
    _, client = service
    response = client.get("/health/live", headers={"X-Correlation-ID": "invalid value"})

    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] != "invalid value"


def test_duplicate_correlation_headers_are_replaced(service: tuple[str, TestClient]) -> None:
    _, client = service
    response = client.get(
        "/health/live",
        headers=[("X-Correlation-ID", "first-value"), ("X-Correlation-ID", "second-value")],
    )

    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] not in {"first-value", "second-value"}


def test_every_opsmind_service_exposes_http_telemetry(service: tuple[str, TestClient]) -> None:
    _, client = service
    client.get("/health/live")
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "http_requests_total" in response.text
    assert "http_request_duration_seconds_bucket" in response.text


def test_api_readiness_returns_503_when_database_is_unavailable() -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @sqlalchemy_event.listens_for(engine, "before_cursor_execute")
    def fail_database_query(*_: object) -> None:
        raise OperationalError("SELECT 1", {}, RuntimeError("database unavailable"))

    sessions = sessionmaker(bind=engine, class_=Session)
    with TestClient(create_app(session_factory=sessions)) as client:
        response = client.get("/health/ready")
    engine.dispose()

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "service": "api"}


def test_api_database_request_returns_sanitized_503_when_storage_is_unavailable() -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @sqlalchemy_event.listens_for(engine, "before_cursor_execute")
    def fail_database_query(*_: object) -> None:
        raise OperationalError("SELECT incidents", {}, RuntimeError("database unavailable"))

    class TestAuthentication:
        def authenticate(self, _request: Request) -> Principal:
            return Principal(uuid4(), uuid4(), frozenset({"incidents:read"}))

    sessions = sessionmaker(bind=engine, class_=Session)
    app = create_app(session_factory=sessions, authentication_backend=TestAuthentication())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/incidents", headers={"X-Correlation-ID": "db-outage-test"})
    engine.dispose()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert response.json()["error"]["correlation_id"] == "db-outage-test"
    assert "database unavailable" not in response.text


@pytest.mark.parametrize(
    ("service_name", "factory"),
    [
        ("incident-engine", create_incident_app),
        ("remediation-engine", create_remediation_app),
    ],
)
def test_database_backed_service_readiness_returns_503_when_database_is_unavailable(
    service_name: str, factory: Callable[..., ASGIApp]
) -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @sqlalchemy_event.listens_for(engine, "before_cursor_execute")
    def fail_database_query(*_: object) -> None:
        raise OperationalError("SELECT 1", {}, RuntimeError("database unavailable"))

    sessions = sessionmaker(bind=engine, class_=Session)
    app = factory(session_factory=sessions)
    with TestClient(app) as client:
        response = client.get("/health/ready")
    engine.dispose()

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "service": service_name}


def test_correlation_id_is_returned_for_unhandled_errors() -> None:
    fastapi_app = FastAPI()

    @fastapi_app.get("/failure")
    async def failure() -> dict[str, str]:
        raise RuntimeError("expected test failure")

    with TestClient(CorrelationIdMiddleware(fastapi_app), raise_server_exceptions=False) as client:
        response = client.get("/failure", headers={"X-Correlation-ID": "failure-case-1"})

    assert response.status_code == 500
    assert response.headers["X-Correlation-ID"] == "failure-case-1"
