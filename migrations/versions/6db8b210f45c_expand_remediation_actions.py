"""add actor, execution result, and compensation data to remediation actions

Revision ID: 6db8b210f45c
Revises: 4a76c1d8b302
Create Date: 2026-10-06 00:00:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "6db8b210f45c"
down_revision: str | None = "4a76c1d8b302"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.types.TypeEngine[Any]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("remediation_actions") as batch:
        batch.add_column(sa.Column("version", sa.Integer(), server_default="1", nullable=False))
        batch.add_column(sa.Column("requested_by", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("execution_start", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("execution_end", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("result", _json_type(), nullable=True))
        batch.add_column(sa.Column("rationale", sa.Text(), server_default="", nullable=False))
        batch.add_column(sa.Column("compensates_action_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_remediation_compensates_action",
            "remediation_actions",
            ["compensates_action_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("remediation_actions") as batch:
        batch.drop_constraint("fk_remediation_compensates_action", type_="foreignkey")
        batch.drop_column("compensates_action_id")
        batch.drop_column("rationale")
        batch.drop_column("result")
        batch.drop_column("execution_end")
        batch.drop_column("execution_start")
        batch.drop_column("requested_by")
        batch.drop_column("version")
