"""add incident creation idempotency receipts

Revision ID: 5b3ce57c90d1
Revises: 2d9163f067aa
Create Date: 2026-10-03 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b3ce57c90d1"
down_revision: str | None = "2d9163f067aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incident_create_idempotency",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "idempotency_key"),
    )
    op.create_index(
        "ix_incident_create_idempotency_incident",
        "incident_create_idempotency",
        ["incident_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_incident_create_idempotency_incident", table_name="incident_create_idempotency"
    )
    op.drop_table("incident_create_idempotency")
