"""Alembic schema migration smoke test on a disposable SQL database."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_initial_migration_creates_domain_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure the initial migration creates all domain tables and matches metadata."""
    database_url = f"sqlite:///{tmp_path / 'migration-test.db'}"
    monkeypatch.setenv("OPSMIND_DATABASE_URL", database_url)
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    command.upgrade(config, "head")
    command.check(config)

    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    expected = {
        "services",
        "service_dependencies",
        "incidents",
        "incident_events",
        "processed_events",
        "incident_create_idempotency",
        "incident_evidence",
        "incident_hypotheses",
        "deployments",
        "remediation_actions",
        "remediation_executions",
        "remediation_approval_requests",
        "notifications",
        "audit_logs",
        "metric_snapshots",
        "log_records",
        "incident_timeline",
        "incident_hypothesis_evidence",
        "investigation_runs",
        "investigation_run_steps",
        "historical_incidents",
    }
    assert expected <= tables
