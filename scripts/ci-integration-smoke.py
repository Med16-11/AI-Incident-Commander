"""Verify the real PostgreSQL schema and Redis Streams services used by CI."""

from __future__ import annotations

import os
from uuid import uuid4

from redis import Redis
from sqlalchemy import create_engine, inspect


def main() -> None:
    database_url = os.environ["OPSMIND_DATABASE_URL"]
    redis_url = os.environ["OPSMIND_REDIS_URL"]

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        tables = set(inspect(engine).get_table_names())
        required_tables = {"incidents", "incident_events", "processed_events", "audit_logs"}
        missing = required_tables - tables
        if missing:
            raise RuntimeError(f"PostgreSQL schema is missing required tables: {sorted(missing)}")
    finally:
        engine.dispose()

    client = Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5)
    stream = f"opsmind:ci-smoke:{uuid4()}"
    try:
        if not client.ping():
            raise RuntimeError("Redis PING did not return success")
        event_id = client.xadd(
            stream, {"event_type": "CiSmokeCheck", "correlation_id": str(uuid4())}
        )
        messages = client.xrange(stream, min=event_id, max=event_id)
        if not messages or messages[0][1].get("event_type") != "CiSmokeCheck":
            raise RuntimeError("Redis Streams write/read smoke check failed")
    finally:
        client.delete(stream)
        client.close()


if __name__ == "__main__":
    main()
