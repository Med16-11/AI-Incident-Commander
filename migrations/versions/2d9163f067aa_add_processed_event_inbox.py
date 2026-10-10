"""add transactional processed-event inbox

Revision ID: 2d9163f067aa
Revises: e8ac0a707755
Create Date: 2026-10-03 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2d9163f067aa"
down_revision: str | None = "e8ac0a707755"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processed_events",
        sa.Column("consumer_group", sa.String(length=160), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("consumer_group", "event_id"),
    )
    op.create_index("ix_processed_events_created", "processed_events", ["processed_at"])


def downgrade() -> None:
    op.drop_index("ix_processed_events_created", table_name="processed_events")
    op.drop_table("processed_events")
