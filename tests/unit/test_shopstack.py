"""Fast local checks for ShopStack configuration and HTTP instrumentation."""

from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from shopstack.config import FaultSettings, ShopStackSettings
from shopstack.main import DownstreamFailure, _create_app, _proxy_error, app
from shopstack.models import Base, OrderRow
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker


def test_shopstack_failure_injection_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "SHOPSTACK_FAILURE_LATENCY_MS",
        "SHOPSTACK_FAILURE_HTTP_500_RATE",
        "SHOPSTACK_FAILURE_HTTP_429_RATE",
        "SHOPSTACK_FAILURE_MEMORY_MB",
        "SHOPSTACK_FAILURE_CPU_STRESS_MS",
        "SHOPSTACK_FAILURE_DEPENDENCY_TIMEOUT_MS",
        "SHOPSTACK_FAILURE_DB_CONNECTIONS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert FaultSettings.from_environment() == FaultSettings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SHOPSTACK_FAILURE_LATENCY_MS", "-1"),
        ("SHOPSTACK_FAILURE_MEMORY_MB", "-1"),
        ("SHOPSTACK_FAILURE_HTTP_500_RATE", "1.1"),
        ("SHOPSTACK_FAILURE_HTTP_429_RATE", "-0.1"),
    ],
)
def test_shopstack_rejects_invalid_failure_configuration(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        FaultSettings.from_environment()


def test_shopstack_http_failure_rates_cannot_exceed_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOPSTACK_FAILURE_HTTP_500_RATE", "0.8")
    monkeypatch.setenv("SHOPSTACK_FAILURE_HTTP_429_RATE", "0.3")
    with pytest.raises(ValueError):
        FaultSettings.from_environment()


def test_shopstack_health_metrics_and_correlation_id() -> None:
    with TestClient(app) as client:
        health = client.get("/health", headers={"X-Request-ID": "shopstack-test-123"})
        assert health.status_code == 200
        assert health.headers["x-request-id"] == "shopstack-test-123"
        assert health.headers["x-correlation-id"] == "shopstack-test-123"
        assert health.json()["status"] == "ok"

        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert (
            'http_requests_total{method="GET",route="/health",service="api-gateway"' in metrics.text
        )
        assert "http_request_duration_seconds_bucket" in metrics.text
        assert "dependency_requests_total" in metrics.text
        assert "database_connections_checked_out" in metrics.text
        assert "redis_operation_duration_seconds" in metrics.text


def test_shopstack_validation_errors_include_correlation_without_input_values() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/orders",
            headers={"X-Correlation-ID": "shopstack-invalid-order"},
            json={"user_id": "sensitive-invalid-user"},
        )

    assert response.status_code == 422
    assert response.headers["x-correlation-id"] == "shopstack-invalid-order"
    assert response.json()["error"]["correlation_id"] == "shopstack-invalid-order"
    assert "sensitive-invalid-user" not in response.text


def test_shopstack_timeout_marks_downstream_side_effect_as_ambiguous() -> None:
    failure = _proxy_error(httpx.ReadTimeout("payment response timed out"), "payment-service")

    assert isinstance(failure, DownstreamFailure)
    assert failure.status_code == 504
    assert failure.ambiguous is True


def test_ambiguous_inventory_failure_does_not_mark_order_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'shopstack.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    monkeypatch.setattr("shopstack.main.create_shopstack_engine", lambda _: engine)
    monkeypatch.setattr("shopstack.main.create_shopstack_sessions", lambda _: sessions)

    def fail_inventory(
        _client: httpx.Client,
        method: str,
        _url: str,
        *,
        dependency: str,
        headers: dict[str, str],
        json_body_value: dict[str, object] | None = None,
    ) -> httpx.Response:
        del headers, json_body_value
        if dependency == "inventory-service" and method == "POST":
            raise DownstreamFailure(502, "inventory response was lost", ambiguous=True)
        return httpx.Response(200)

    monkeypatch.setattr("shopstack.main._call_service", fail_inventory)
    settings = ShopStackSettings(
        service_name="order-service",
        port=8000,
        database_url="sqlite://",
        redis_url="redis://localhost:6379/1",
        user_service_url="http://user-service",
        order_service_url="http://order-service",
        inventory_service_url="http://inventory-service",
        payment_service_url="http://payment-service",
        otel_endpoint=None,
        otel_insecure=False,
        faults=FaultSettings(),
    )
    application = _create_app(settings)
    client = TestClient(application, raise_server_exceptions=False)
    response = client.post(
        "/orders",
        headers={"Idempotency-Key": "ambiguous-inventory"},
        json={
            "user_id": str(UUID("11111111-1111-4111-8111-111111111111")),
            "sku": "SKU-COFFEE",
            "quantity": 1,
            "amount": str(Decimal("9.99")),
        },
    )

    assert response.status_code == 502
    with sessions() as session:
        order = session.scalar(select(OrderRow))
        assert order is not None
        assert order.status == "COMPENSATION_REQUIRED"
    engine.dispose()
