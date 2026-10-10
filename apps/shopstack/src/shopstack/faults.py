"""Request fault injection used only when explicitly enabled by environment."""

import asyncio
import random
import time

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

from shopstack.config import FaultSettings

logger = structlog.get_logger(service="shopstack")


async def inject_request_fault(request: Request, settings: FaultSettings) -> JSONResponse | None:
    """Apply latency, CPU stress, and probabilistic HTTP faults to business routes."""
    if request.url.path in {"/health", "/ready", "/metrics"}:
        return None
    if settings.latency_ms:
        await asyncio.sleep(settings.latency_ms / 1000)
    if settings.cpu_stress_ms:
        deadline = time.perf_counter() + settings.cpu_stress_ms / 1000
        while time.perf_counter() < deadline:
            pass
    if settings.packet_loss_rate and random.random() < settings.packet_loss_rate:
        logger.error(
            "injected_packet_loss",
            service=request.app.state.settings.service_name,
        )
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "1", "X-ShopStack-Fault": "packet-loss"},
            content={
                "error": {
                    "code": "injected_packet_loss",
                    "message": "ShopStack simulated a dropped request",
                }
            },
        )
    selection = random.random()
    if selection < settings.http_429_rate:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "1"},
            content={
                "error": {
                    "code": "injected_rate_limit",
                    "message": "ShopStack failure injection returned HTTP 429",
                }
            },
        )
    if selection < settings.http_429_rate + settings.http_500_rate:
        logger.error(
            "injected_http_500",
            service=request.app.state.settings.service_name,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "injected_internal_error",
                    "message": "ShopStack failure injection returned HTTP 500",
                }
            },
        )
    return None
