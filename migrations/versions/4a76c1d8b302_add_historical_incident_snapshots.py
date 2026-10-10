"""store resolved incident snapshots for historical retrieval

Revision ID: 4a76c1d8b302
Revises: 9c4d2a71e630
Create Date: 2026-10-06 00:00:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4a76c1d8b302"
down_revision: str | None = "9c4d2a71e630"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine[Any]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "historical_incidents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("symptoms", _json_type(), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=False),
        sa.Column("affected_services", _json_type(), nullable=False),
        sa.Column("evidence", _json_type(), nullable=False),
        sa.Column("remediation", _json_type(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution_seconds", sa.Float(), nullable=False),
        sa.Column("tags", _json_type(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("resolution_seconds >= 0", name="ck_historical_resolution_nonnegative"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "incident_id", name="uq_historical_incident_scope"),
    )
    op.create_index(
        "ix_historical_tenant_resolved", "historical_incidents", ["tenant_id", "resolved_at"]
    )
    op.create_index(
        "ix_historical_tenant_service", "historical_incidents", ["tenant_id", "service_id"]
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_historical_incident_search ON historical_incidents "
            "USING gin (to_tsvector('english', search_text))"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index("ix_historical_incident_search", table_name="historical_incidents")
    op.drop_index("ix_historical_tenant_service", table_name="historical_incidents")
    op.drop_index("ix_historical_tenant_resolved", table_name="historical_incidents")
    op.drop_table("historical_incidents")
