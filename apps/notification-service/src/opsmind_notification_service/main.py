"""Slack notification subscriber and authenticated interactive action endpoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID

import structlog
from fastapi import FastAPI, Header, HTTPException, Request
from opsmind_common.config import Settings
from opsmind_common.correlation import CorrelationIdMiddleware
from opsmind_common.logging import configure_logging
from opsmind_events.consumer import RedisStreamConsumer
from opsmind_events.metrics import EventMetrics
from opsmind_events.redis_client import RedisStreamsClient
from opsmind_observability.metrics import ServiceMetrics
from opsmind_storage.database import create_database_engine, create_session_factory
from opsmind_storage.event_inbox import SqlAlchemyEventUnitOfWorkFactory
from redis import Redis
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from opsmind_notification_service.slack import (
    HttpRemediationDecisionPort,
    RemediationDecisionPort,
    SlackAdapter,
    SlackIdentityResolver,
    SlackIncidentEventHandler,
    SlackInteractionService,
    SlackUserTokenMap,
    SlackWebApiAdapter,
)


class RedisSlackThreadStore:
    """Persist incident thread roots in Redis so notification replicas can share them."""

    def __init__(self, client: Redis) -> None:
        self._redis = client

    def get_thread(self, incident_id: UUID) -> str | None:
        value = self._redis.get(f"opsmind:slack:incident:{incident_id}")
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return value if isinstance(value, str) else None

    def set_thread(self, incident_id: UUID, thread_ts: str) -> None:
        self._redis.set(f"opsmind:slack:incident:{incident_id}", thread_ts)


def create_app(
    *,
    settings: Settings | None = None,
    adapter: SlackAdapter | None = None,
    identity_resolver: SlackIdentityResolver | None = None,
    decision_port: RemediationDecisionPort | None = None,
    redis_client: Redis | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> ASGIApp:
    """Compose optional Slack delivery and signed actions behind replaceable ports."""
    configuration = settings or Settings()
    configure_logging(configuration)
    logger = structlog.get_logger(service="notification-service")
    metrics = ServiceMetrics("notification-service")
    owns_engine = session_factory is None
    engine = create_database_engine(configuration.database_url) if owns_engine else None
    if session_factory is None:
        assert engine is not None
        sessions = create_session_factory(engine)
    else:
        sessions = session_factory

    bot_token = (
        configuration.slack_bot_token.get_secret_value()
        if configuration.slack_bot_token is not None
        else None
    )
    signing_secret = (
        configuration.slack_signing_secret.get_secret_value()
        if configuration.slack_signing_secret is not None
        else None
    )
    slack_user_tokens = (
        configuration.slack_user_tokens.get_secret_value().strip()
        if configuration.slack_user_tokens is not None
        else ""
    )
    if identity_resolver is None and slack_user_tokens:
        try:
            token_map = json.loads(slack_user_tokens)
            if not isinstance(token_map, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in token_map.items()
            ):
                raise ValueError("expected a JSON object of Slack identity keys to bearer tokens")
            identity_resolver = SlackUserTokenMap(token_map)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("OPSMIND_SLACK_USER_TOKENS is not a valid identity map") from exc
    owns_decision_port = False
    if decision_port is None and configuration.slack_remediation_api_url:
        decision_port = HttpRemediationDecisionPort(
            configuration.slack_remediation_api_url,
            allow_insecure_http=configuration.environment in {"local", "dev"},
        )
        owns_decision_port = True
    active_adapter = adapter
    if active_adapter is None and bot_token:
        active_adapter = SlackWebApiAdapter(bot_token)
    interactions = None
    if signing_secret:
        if identity_resolver is None:
            logger.warning("slack_actions_disabled", reason="identity resolver is not configured")
        elif decision_port is None:
            logger.warning("slack_actions_disabled", reason="remediation API is not configured")
        else:
            interactions = SlackInteractionService(
                signing_secret,
                identity_resolver,
                decision_port,
            )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        owned_redis = redis_client is None
        redis = redis_client or Redis.from_url(configuration.redis_url, decode_responses=True)
        stop = asyncio.Event()
        consumer_task: asyncio.Task[None] | None = None
        if active_adapter is not None and configuration.slack_incident_channel:
            consumer = RedisStreamConsumer(
                cast(RedisStreamsClient, redis),
                "opsmind:events",
                "opsmind-notification-service",
                "notification-service",
                SlackIncidentEventHandler(
                    active_adapter,
                    RedisSlackThreadStore(redis),
                    configuration.slack_incident_channel,
                ),
                SqlAlchemyEventUnitOfWorkFactory(sessions),
                metrics=EventMetrics(registry=metrics.registry),
            )

            async def consume() -> None:
                while not stop.is_set():
                    try:
                        await asyncio.to_thread(consumer.consume_once, block_ms=1000)
                    except Exception as exc:
                        logger.error("slack_event_consumer_failed", error_type=type(exc).__name__)
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=1.0)
                        except TimeoutError:
                            continue

            consumer_task = asyncio.create_task(consume())
            logger.info(
                "slack_event_consumer_started", channel=configuration.slack_incident_channel
            )
        else:
            logger.info(
                "slack_delivery_disabled",
                reason="bot token or incident channel not configured",
            )
        try:
            yield
        finally:
            stop.set()
            if consumer_task is not None:
                await consumer_task
            if active_adapter is not None and isinstance(active_adapter, SlackWebApiAdapter):
                active_adapter.close()
            if owns_decision_port and isinstance(decision_port, HttpRemediationDecisionPort):
                decision_port.close()
            if owned_redis:
                redis.close()
            if engine is not None:
                engine.dispose()

    app = FastAPI(
        title="OpsMind Notification Service",
        description="Slack incident updates and signed human remediation decisions.",
        version="0.1.0",
        lifespan=lifespan,
    )
    metrics.install(app)

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "ok", "service": "notification-service"}

    @app.get("/health/ready")
    async def readiness() -> dict[str, str]:
        return {"status": "ready", "service": "notification-service"}

    @app.post("/slack/interactions")
    async def slack_interactions(
        request: Request,
        timestamp: str | None = Header(default=None, alias="X-Slack-Request-Timestamp"),
        signature: str | None = Header(default=None, alias="X-Slack-Signature"),
    ) -> JSONResponse:
        if interactions is None:
            raise HTTPException(503, "Slack interactive actions are not configured")
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > 256_000:
                    raise HTTPException(413, "Slack interaction payload is too large")
            except ValueError as exc:
                raise HTTPException(400, "Invalid content length") from exc
        body = await request.body()
        if len(body) > 256_000:
            raise HTTPException(413, "Slack interaction payload is too large")
        await asyncio.to_thread(interactions.handle, body, timestamp, signature)
        return JSONResponse({"text": "Decision recorded. OpsMind is processing the request."})

    return CorrelationIdMiddleware(app)


app = create_app()
