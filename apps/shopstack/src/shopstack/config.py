"""Environment-backed configuration and bounded fault injection settings."""

import os
from dataclasses import dataclass

from sqlalchemy.engine import URL


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True, slots=True)
class FaultSettings:
    """Per-service failure injection controls; all are inert by default."""

    latency_ms: int = 0
    http_500_rate: float = 0.0
    http_429_rate: float = 0.0
    memory_mb: int = 0
    cpu_stress_ms: int = 0
    dependency_timeout_ms: int = 0
    database_connections: int = 0
    packet_loss_rate: float = 0.0
    crash_on_start: bool = False

    @classmethod
    def from_environment(cls) -> "FaultSettings":
        values = cls(
            latency_ms=_int_env("SHOPSTACK_FAILURE_LATENCY_MS", 0),
            http_500_rate=_float_env("SHOPSTACK_FAILURE_HTTP_500_RATE", 0.0),
            http_429_rate=_float_env("SHOPSTACK_FAILURE_HTTP_429_RATE", 0.0),
            memory_mb=_int_env("SHOPSTACK_FAILURE_MEMORY_MB", 0),
            cpu_stress_ms=_int_env("SHOPSTACK_FAILURE_CPU_STRESS_MS", 0),
            dependency_timeout_ms=_int_env("SHOPSTACK_FAILURE_DEPENDENCY_TIMEOUT_MS", 0),
            database_connections=_int_env("SHOPSTACK_FAILURE_DB_CONNECTIONS", 0),
            packet_loss_rate=_float_env("SHOPSTACK_FAILURE_PACKET_LOSS_RATE", 0.0),
            crash_on_start=_bool_env("SHOPSTACK_FAILURE_CRASH_ON_START", False),
        )
        if (
            min(
                values.latency_ms,
                values.memory_mb,
                values.cpu_stress_ms,
                values.dependency_timeout_ms,
                values.database_connections,
            )
            < 0
        ):
            raise ValueError("failure injection sizes and durations cannot be negative")
        if not 0 <= values.http_500_rate <= 1 or not 0 <= values.http_429_rate <= 1:
            raise ValueError("HTTP failure rates must be between 0 and 1")
        if values.http_500_rate + values.http_429_rate > 1:
            raise ValueError("HTTP failure rates must sum to at most 1")
        if not 0 <= values.packet_loss_rate <= 1:
            raise ValueError("packet loss rate must be between 0 and 1")
        return values


@dataclass(frozen=True, slots=True)
class ShopStackSettings:
    service_name: str
    port: int
    database_url: str
    redis_url: str
    user_service_url: str
    order_service_url: str
    inventory_service_url: str
    payment_service_url: str
    otel_endpoint: str | None
    otel_insecure: bool
    faults: FaultSettings

    @classmethod
    def from_environment(cls) -> "ShopStackSettings":
        db_user = os.getenv("SHOPSTACK_DB_USER", "shopstack")
        db_password = os.getenv("SHOPSTACK_DB_PASSWORD") or None
        db_host = os.getenv("SHOPSTACK_DB_HOST", "localhost")
        db_port = _int_env("SHOPSTACK_DB_PORT", 5432)
        db_name = os.getenv("SHOPSTACK_DB_NAME", "shopstack")
        database_url = URL.create(
            "postgresql+psycopg",
            username=db_user,
            password=db_password,
            host=db_host,
            port=db_port,
            database=db_name,
        )
        return cls(
            service_name=os.getenv("SHOPSTACK_SERVICE", "api-gateway"),
            port=_int_env("SHOPSTACK_PORT", 8000),
            database_url=database_url.render_as_string(hide_password=False),
            redis_url=os.getenv("SHOPSTACK_REDIS_URL", "redis://localhost:6379/1"),
            user_service_url=os.getenv("SHOPSTACK_USER_SERVICE_URL", "http://localhost:8101"),
            order_service_url=os.getenv("SHOPSTACK_ORDER_SERVICE_URL", "http://localhost:8102"),
            inventory_service_url=os.getenv(
                "SHOPSTACK_INVENTORY_SERVICE_URL", "http://localhost:8103"
            ),
            payment_service_url=os.getenv("SHOPSTACK_PAYMENT_SERVICE_URL", "http://localhost:8104"),
            otel_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            otel_insecure=_bool_env("OTEL_EXPORTER_OTLP_INSECURE", False),
            faults=FaultSettings.from_environment(),
        )
