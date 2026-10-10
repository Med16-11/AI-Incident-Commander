"""persist expiring remediation approval requests and decisions

Revision ID: 9710f41ad8e2
Revises: 6db8b210f45c
Create Date: 2026-10-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9710f41ad8e2"
down_revision: str | None = "6db8b210f45c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "remediation_approval_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(length=160), nullable=False),
        sa.Column("target", sa.String(length=1000), nullable=False),
        sa.Column("risk_level", sa.String(length=20), nullable=False),
        sa.Column("request_reason", sa.Text(), nullable=False),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("approver_id", sa.Uuid(), nullable=True),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "decision IN ('requested','approved','rejected','expired')",
            name="ck_remediation_approval_decision",
        ),
        sa.ForeignKeyConstraint(["action_id"], ["remediation_actions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("action_id", name="uq_remediation_approval_action"),
    )
    op.create_index(
        "ix_approval_requests_tenant_expiry",
        "remediation_approval_requests",
        ["tenant_id", "decision", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_approval_requests_tenant_expiry", table_name="remediation_approval_requests")
    op.drop_table("remediation_approval_requests")
