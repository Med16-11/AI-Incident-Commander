"""Structured JSON logging configuration."""

import logging

import structlog
from opentelemetry import trace
from structlog.typing import EventDict

from opsmind_common.config import Settings


def _add_trace_context(_: object, __: str, event_dict: EventDict) -> EventDict:
    """Add active trace identifiers so logs can be joined to spans."""
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = f"{context.trace_id:032x}"
        event_dict["span_id"] = f"{context.span_id:016x}"
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Configure stdlib and application logs with consistent structured fields."""
    logging.basicConfig(
        format="%(message)s",
        level=settings.log_level.upper(),
    )
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_trace_context,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
