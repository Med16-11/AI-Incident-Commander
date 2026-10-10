"""Configurable ShopStack services used as a realistic monitored demo."""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from opentelemetry.sdk.trace import TracerProvider
from opsmind_common.config import Settings
from opsmind_common.logging import configure_logging
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from shopstack.config import ShopStackSettings
from shopstack.db import (
    create_shopstack_engine,
    create_shopstack_sessions,
    initialize_database,
)
from shopstack.errors import http_error_handler, validation_error_handler
from shopstack.faults import inject_request_fault
from shopstack.models import (
    InventoryItemRow,
    InventoryReservationRow,
    OrderRow,
    PaymentRow,
    UserRow,
)
from shopstack.schemas import (
    OrderRequest,
    OrderResponse,
    PaymentRequest,
    PaymentResponse,
    ReservationRequest,
    ReservationResponse,
    UserResponse,
)
from shopstack.telemetry import (
    ServiceMetrics,
    _observe_dependency,
    configure_tracing,
    current_metrics,
)

configure_logging(Settings())
logger = structlog.get_logger(service="shopstack")

DB_SERVICES = {"user-service", "order-service", "inventory-service", "payment-service"}
SERVICE_NAMES = {"api-gateway", *DB_SERVICES}
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


def _outbound_timeout(settings: ShopStackSettings) -> float:
    if settings.faults.dependency_timeout_ms:
        return settings.faults.dependency_timeout_ms / 1000
    return 4.0


def _request_headers(request: Request) -> dict[str, str]:
    correlation_id = getattr(request.state, "correlation_id", None)
    if correlation_id is None:
        correlation_id = request.headers.get("x-correlation-id") or request.headers.get(
            "x-request-id"
        )
    correlation_id = correlation_id or str(uuid4())
    return {"X-Correlation-ID": correlation_id, "X-Request-ID": correlation_id}


def _request_hash(body: OrderRequest) -> str:
    encoded = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode()).hexdigest()


class DownstreamFailure(HTTPException):
    """Downstream request failure with an explicit side-effect certainty flag."""

    def __init__(self, status_code: int, detail: str, *, ambiguous: bool) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.ambiguous = ambiguous


def _proxy_error(exc: httpx.HTTPError, dependency: str) -> DownstreamFailure:
    if isinstance(exc, httpx.TimeoutException):
        return DownstreamFailure(504, f"{dependency} timed out", ambiguous=True)
    if isinstance(exc, httpx.ConnectError):
        return DownstreamFailure(503, f"{dependency} is unavailable", ambiguous=False)
    return DownstreamFailure(502, f"{dependency} returned an invalid response", ambiguous=True)


def _call_service(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    dependency: str,
    headers: dict[str, str],
    json_body_value: dict[str, Any] | None = None,
) -> httpx.Response:
    started = time.perf_counter()
    try:
        response = client.request(method, url, json=json_body_value, headers=headers)
    except httpx.HTTPError as exc:
        _observe_dependency(dependency, method.lower(), started, "error")
        raise _proxy_error(exc, dependency) from exc
    _observe_dependency(
        dependency, method.lower(), started, "error" if response.is_error else "success"
    )
    if response.status_code >= 500:
        raise DownstreamFailure(502, f"{dependency} request failed", ambiguous=True)
    if response.status_code == 429:
        raise DownstreamFailure(502, f"{dependency} request failed", ambiguous=False)
    if response.is_error:
        try:
            detail = response.json().get("detail", f"{dependency} rejected the request")
        except (ValueError, AttributeError):
            detail = f"{dependency} rejected the request"
        raise HTTPException(status_code=response.status_code, detail=detail)
    return response


def _redis_operation(metrics: ServiceMetrics, operation: str, callback: Any, *args: Any) -> Any:
    started = time.perf_counter()
    try:
        result = callback(*args)
    except RedisError:
        elapsed = time.perf_counter() - started
        metrics.redis_duration.labels(metrics.service_name, operation, "error").observe(elapsed)
        metrics.redis_failures.labels(metrics.service_name, operation).inc()
        raise
    metrics.redis_duration.labels(metrics.service_name, operation, "success").observe(
        time.perf_counter() - started
    )
    return result


def _create_app(settings: ShopStackSettings) -> FastAPI:
    if settings.service_name not in SERVICE_NAMES:
        raise ValueError(f"SHOPSTACK_SERVICE must be one of {sorted(SERVICE_NAMES)}")

    engine: Engine | None = (
        create_shopstack_engine(settings) if settings.service_name in DB_SERVICES else None
    )
    sessions: sessionmaker[Session] | None = (
        create_shopstack_sessions(engine) if engine is not None else None
    )
    redis_client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
    )
    http_client = httpx.Client(timeout=_outbound_timeout(settings))
    metrics = ServiceMetrics()
    metrics.service_name = settings.service_name
    if engine is not None:
        metrics.instrument_engine(engine, settings.service_name)
    memory_reservation = bytearray(settings.faults.memory_mb * 1024 * 1024)
    for page in range(0, len(memory_reservation), 4096):
        memory_reservation[page] = 1
    tracer_provider: TracerProvider

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.faults.crash_on_start:
            raise RuntimeError("ShopStack chaos experiment requested startup crash")
        if engine is not None:
            from starlette.concurrency import run_in_threadpool

            await run_in_threadpool(initialize_database, engine)
        logger.info("service_started", service_name=settings.service_name)
        yield
        http_client.close()
        redis_client.close()
        if engine is not None:
            engine.dispose()
        tracer_provider.shutdown()
        logger.info("service_stopped", service_name=settings.service_name)

    app = FastAPI(
        title=f"ShopStack {settings.service_name}",
        description="Demo commerce service with configurable operational failure injection.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = sessions
    app.state.redis = redis_client
    app.state.http_client = http_client
    app.state.memory_reservation = memory_reservation
    app.state.metrics = metrics
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, cast(Any, validation_error_handler))

    @app.middleware("http")
    async def request_middleware(request: Request, call_next: Any) -> Response:
        metrics_token = current_metrics.set(metrics)
        started = time.perf_counter()
        collect_metrics = request.url.path != "/metrics"
        if collect_metrics:
            metrics.in_flight.labels(settings.service_name).inc()
        status_code = 500
        candidate_request_id = request.headers.get("x-correlation-id") or request.headers.get(
            "x-request-id", ""
        )
        request_id = (
            candidate_request_id
            if REQUEST_ID_PATTERN.fullmatch(candidate_request_id)
            else str(uuid4())
        )
        request.state.request_id = request_id
        request.state.correlation_id = request_id
        structlog.contextvars.bind_contextvars(
            service=settings.service_name,
            correlation_id=request_id,
        )
        try:
            injected = await inject_request_fault(request, settings.faults)
            response = injected if injected is not None else await call_next(request)
            status_code = response.status_code
            response.headers["X-Correlation-ID"] = request_id
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception as exc:
            logger.error(
                "unhandled_shopstack_error",
                service_name=settings.service_name,
                error_type=type(exc).__name__,
            )
            status_code = 500
            return JSONResponse(
                status_code=500,
                headers={"X-Correlation-ID": request_id, "X-Request-ID": request_id},
                content={
                    "error": {
                        "code": "internal_error",
                        "message": "An internal error occurred",
                        "correlation_id": request_id,
                    }
                },
            )
        finally:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            duration = time.perf_counter() - started
            if collect_metrics:
                metrics.duration.labels(settings.service_name, request.method, route).observe(
                    duration
                )
                metrics.requests.labels(
                    settings.service_name, request.method, route, str(status_code)
                ).inc()
                metrics.in_flight.labels(settings.service_name).dec()
            current_metrics.reset(metrics_token)
            structlog.contextvars.clear_contextvars()

    @app.get("/health", summary="Liveness health check")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": settings.service_name}

    @app.get("/ready", summary="Dependency readiness check", response_model=None)
    def ready(request: Request) -> Response | dict[str, str]:
        try:
            if engine is not None:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
            if settings.service_name == "api-gateway":
                _redis_operation(metrics, "ping", redis_client.ping)
                for name, url in (
                    ("user-service", settings.user_service_url),
                    ("order-service", settings.order_service_url),
                ):
                    check = http_client.get(f"{url}/health", headers=_request_headers(request))
                    if check.is_error:
                        raise RuntimeError(f"{name} is not ready")
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "service": settings.service_name},
            )
        return {"status": "ready", "service": settings.service_name}

    @app.get("/metrics", summary="Prometheus metrics", response_class=PlainTextResponse)
    def prometheus_metrics() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    def get_session(request: Request) -> Iterator[Session]:
        factory: sessionmaker[Session] | None = request.app.state.session_factory
        if factory is None:
            raise RuntimeError("This service has no database session factory")
        with factory() as session:
            yield session

    def hold_database_connections(request: Request) -> Iterator[None]:
        configured_engine: Engine | None = request.app.state.engine
        count = settings.faults.database_connections
        connections = []
        if configured_engine is not None:
            try:
                for _ in range(min(count, 5)):
                    connections.append(configured_engine.connect())
                yield
            finally:
                for connection in reversed(connections):
                    connection.close()
        else:
            yield

    if settings.service_name == "user-service":

        @app.get(
            "/users/{user_id}",
            response_model=UserResponse,
            dependencies=[Depends(hold_database_connections)],
        )
        def get_user(
            user_id: UUID,
            session: Session = Depends(get_session),  # noqa: B008
        ) -> UserResponse:
            row = session.get(UserRow, user_id)
            if row is None:
                raise HTTPException(status_code=404, detail="User was not found")
            return UserResponse.model_validate(row)

    elif settings.service_name == "inventory-service":

        @app.get("/items/{sku}", dependencies=[Depends(hold_database_connections)])
        def get_inventory(
            sku: str,
            session: Session = Depends(get_session),  # noqa: B008
        ) -> dict[str, Any]:
            item = session.get(InventoryItemRow, sku)
            if item is None:
                raise HTTPException(status_code=404, detail="Inventory item was not found")
            return {"sku": item.sku, "name": item.name, "available": item.available}

        @app.post(
            "/reservations",
            response_model=ReservationResponse,
            dependencies=[Depends(hold_database_connections)],
        )
        def reserve_inventory(
            body: ReservationRequest,
            session: Session = Depends(get_session),  # noqa: B008
        ) -> ReservationResponse:
            with session.begin():
                reservation = session.get(InventoryReservationRow, body.order_id)
                item = session.scalar(
                    select(InventoryItemRow)
                    .where(InventoryItemRow.sku == body.sku)
                    .with_for_update()
                )
                if item is None:
                    raise HTTPException(status_code=404, detail="Inventory item was not found")
                if reservation is None:
                    if item.available < body.quantity:
                        raise HTTPException(status_code=409, detail="Insufficient inventory")
                    item.available -= body.quantity
                    reservation = InventoryReservationRow(
                        order_id=body.order_id,
                        sku=body.sku,
                        quantity=body.quantity,
                        status="reserved",
                    )
                    session.add(reservation)
                elif reservation.sku != body.sku or reservation.quantity != body.quantity:
                    raise HTTPException(status_code=409, detail="Reservation does not match order")
                return ReservationResponse(
                    order_id=reservation.order_id,
                    sku=reservation.sku,
                    quantity=reservation.quantity,
                    status=reservation.status,
                    available=item.available,
                )

        @app.delete("/reservations/{order_id}", dependencies=[Depends(hold_database_connections)])
        def release_inventory(
            order_id: UUID,
            session: Session = Depends(get_session),  # noqa: B008
        ) -> dict[str, str]:
            with session.begin():
                reservation = session.scalar(
                    select(InventoryReservationRow)
                    .where(InventoryReservationRow.order_id == order_id)
                    .with_for_update()
                )
                if reservation is None:
                    raise HTTPException(status_code=404, detail="Reservation was not found")
                if reservation.status == "reserved":
                    item = session.scalar(
                        select(InventoryItemRow)
                        .where(InventoryItemRow.sku == reservation.sku)
                        .with_for_update()
                    )
                    if item is None:
                        raise HTTPException(status_code=500, detail="Reserved item is missing")
                    item.available += reservation.quantity
                    reservation.status = "released"
            return {"order_id": str(order_id), "status": reservation.status}

    elif settings.service_name == "payment-service":

        @app.post(
            "/payments",
            response_model=PaymentResponse,
            dependencies=[Depends(hold_database_connections)],
        )
        def create_payment(
            body: PaymentRequest,
            session: Session = Depends(get_session),  # noqa: B008
        ) -> PaymentResponse:
            with session.begin():
                payment = session.scalar(
                    select(PaymentRow).where(PaymentRow.order_id == body.order_id)
                )
                if payment is None:
                    payment = PaymentRow(
                        order_id=body.order_id,
                        amount=body.amount,
                        status="AUTHORIZED",
                    )
                    session.add(payment)
                    session.flush()
                elif payment.amount != body.amount:
                    raise HTTPException(status_code=409, detail="Payment amount does not match")
            return PaymentResponse(
                id=payment.id,
                order_id=payment.order_id,
                amount=payment.amount,
                status=payment.status,
            )

    elif settings.service_name == "order-service":

        @app.post(
            "/orders",
            response_model=OrderResponse,
            status_code=status.HTTP_201_CREATED,
            dependencies=[Depends(hold_database_connections)],
        )
        def create_order(
            body: OrderRequest,
            request: Request,
            response: Response,
            idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
            session: Session = Depends(get_session),  # noqa: B008
        ) -> OrderResponse:
            request_hash = _request_hash(body)
            with session.begin():
                existing = session.scalar(
                    select(OrderRow).where(OrderRow.idempotency_key == idempotency_key)
                )
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise HTTPException(status_code=409, detail="Idempotency key conflict")
                    response.status_code = (
                        status.HTTP_202_ACCEPTED
                        if existing.status in {"PENDING", "COMPENSATION_REQUIRED"}
                        else status.HTTP_200_OK
                    )
                    return OrderResponse.model_validate(existing)

            headers = _request_headers(request)
            _call_service(
                http_client,
                "GET",
                f"{settings.user_service_url}/users/{body.user_id}",
                dependency="user-service",
                headers=headers,
            )
            order = OrderRow(
                id=uuid4(),
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                user_id=body.user_id,
                sku=body.sku,
                quantity=body.quantity,
                amount=body.amount,
                status="PENDING",
            )
            try:
                with session.begin():
                    session.add(order)
                    session.flush()
            except IntegrityError as exc:
                session.rollback()
                replay = session.scalar(
                    select(OrderRow).where(OrderRow.idempotency_key == idempotency_key)
                )
                if replay is None or replay.request_hash != request_hash:
                    raise HTTPException(status_code=409, detail="Idempotency key conflict") from exc
                response.status_code = status.HTTP_200_OK
                return OrderResponse.model_validate(replay)

            reserved = False
            try:
                _call_service(
                    http_client,
                    "POST",
                    f"{settings.inventory_service_url}/reservations",
                    dependency="inventory-service",
                    headers=headers,
                    json_body_value={
                        "order_id": str(order.id),
                        "sku": body.sku,
                        "quantity": body.quantity,
                    },
                )
                reserved = True
                payment_response = _call_service(
                    http_client,
                    "POST",
                    f"{settings.payment_service_url}/payments",
                    dependency="payment-service",
                    headers=headers,
                    json_body_value={"order_id": str(order.id), "amount": str(body.amount)},
                )
                try:
                    payment = PaymentResponse.model_validate(payment_response.json())
                except (ValueError, TypeError) as exc:
                    raise DownstreamFailure(
                        502,
                        "payment-service returned an invalid response",
                        ambiguous=True,
                    ) from exc
            except HTTPException as exc:
                # Any downstream timeout or 5xx may arrive after its side effect
                # committed, even when this process never observed a success.
                ambiguous_side_effect = bool(getattr(exc, "ambiguous", False))
                compensated = not ambiguous_side_effect
                if reserved and not ambiguous_side_effect:
                    try:
                        _call_service(
                            http_client,
                            "DELETE",
                            f"{settings.inventory_service_url}/reservations/{order.id}",
                            dependency="inventory-service",
                            headers=headers,
                        )
                    except HTTPException:
                        compensated = False
                with session.begin():
                    persisted = session.get(OrderRow, order.id)
                    if persisted is not None:
                        persisted.status = "FAILED" if compensated else "COMPENSATION_REQUIRED"
                if exc.status_code == 409:
                    raise
                raise HTTPException(
                    status_code=502,
                    detail="Order could not complete a downstream service step",
                ) from exc

            with session.begin():
                persisted = session.get(OrderRow, order.id)
                if persisted is None:
                    raise HTTPException(status_code=500, detail="Order record disappeared")
                persisted.status = "CONFIRMED"
                persisted.payment_id = payment.id
            return OrderResponse.model_validate(persisted)

        @app.get(
            "/orders/{order_id}",
            response_model=OrderResponse,
            dependencies=[Depends(hold_database_connections)],
        )
        def get_order(
            order_id: UUID,
            session: Session = Depends(get_session),  # noqa: B008
        ) -> OrderResponse:
            order = session.get(OrderRow, order_id)
            if order is None:
                raise HTTPException(status_code=404, detail="Order was not found")
            return OrderResponse.model_validate(order)

    elif settings.service_name == "api-gateway":

        @app.get("/users/{user_id}")
        def gateway_get_user(user_id: UUID, request: Request) -> Response:
            cache_key = f"shopstack:user:{user_id}"
            try:
                cached = _redis_operation(metrics, "get", redis_client.get, cache_key)
            except RedisError:
                cached = None
            if cached is not None:
                try:
                    cached_user = UserResponse.model_validate_json(cast(str, cached))
                except (RedisError, ValueError, TypeError):
                    logger.warning("redis_cache_entry_invalid")
                else:
                    return JSONResponse(content=cached_user.model_dump(mode="json"))
            result = _call_service(
                http_client,
                "GET",
                f"{settings.user_service_url}/users/{user_id}",
                dependency="user-service",
                headers=_request_headers(request),
            )
            try:
                payload = UserResponse.model_validate(result.json()).model_dump(mode="json")
            except ValueError as exc:
                raise HTTPException(
                    status_code=502, detail="user-service returned an invalid response"
                ) from exc
            try:
                _redis_operation(
                    metrics, "setex", redis_client.setex, cache_key, 30, json.dumps(payload)
                )
            except RedisError:
                logger.warning("redis_cache_write_failed")
            return JSONResponse(content=payload)

        @app.post("/orders", status_code=status.HTTP_201_CREATED)
        def gateway_create_order(
            body: OrderRequest,
            request: Request,
            idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
        ) -> Response:
            result = _call_service(
                http_client,
                "POST",
                f"{settings.order_service_url}/orders",
                dependency="order-service",
                headers={**_request_headers(request), "Idempotency-Key": idempotency_key},
                json_body_value=body.model_dump(mode="json"),
            )
            try:
                order = OrderResponse.model_validate(result.json())
            except (ValueError, TypeError) as exc:
                raise HTTPException(
                    status_code=502, detail="order-service returned an invalid response"
                ) from exc
            return JSONResponse(
                status_code=result.status_code,
                content=order.model_dump(mode="json"),
            )

    tracer_provider = configure_tracing(
        app,
        settings.service_name,
        settings.otel_endpoint,
        settings.otel_insecure,
        http_client,
        engine,
    )
    return app


app = _create_app(ShopStackSettings.from_environment())
