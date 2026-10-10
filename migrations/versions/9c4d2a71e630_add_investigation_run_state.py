"""persist bounded investigation runs and trace steps

Revision ID: 9c4d2a71e630
Revises: 7a8f4c2d91be
Create Date: 2026-10-06 00:00:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9c4d2a71e630"
down_revision: str | None = "7a8f4c2d91be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine[Any]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "investigation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("tool_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("state_data", _json_type(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('INITIAL_TRIAGE','HYPOTHESIS_FORMED','EVIDENCE_COLLECTION',"
            "'HYPOTHESIS_UPDATED','ROOT_CAUSE_IDENTIFIED','REMEDIATION_PROPOSED')",
            name="ck_investigation_run_state",
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING','COMPLETED','FAILED','TIMED_OUT')",
            name="ck_investigation_run_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_investigation_run_version_positive"),
        sa.CheckConstraint("tool_calls >= 0", name="ck_investigation_run_tool_calls_nonnegative"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_investigation_runs_incident_created",
        "investigation_runs",
        ["tenant_id", "incident_id", "started_at"],
    )
    op.create_index(
        "ix_investigation_runs_status_updated", "investigation_runs", ["status", "updated_at"]
    )
    op.create_table(
        "investigation_run_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("outcome", sa.String(length=40), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("details", _json_type(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["investigation_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_investigation_run_step_sequence"),
    )
    op.create_index(
        "ix_investigation_steps_run_created", "investigation_run_steps", ["run_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_investigation_steps_run_created", table_name="investigation_run_steps")
    op.drop_table("investigation_run_steps")
    op.drop_index("ix_investigation_runs_status_updated", table_name="investigation_runs")
    op.drop_index("ix_investigation_runs_incident_created", table_name="investigation_runs")
    op.drop_table("investigation_runs")
