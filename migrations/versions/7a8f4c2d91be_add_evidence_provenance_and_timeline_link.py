"""add evidence provenance fields and timeline linkage

Revision ID: 7a8f4c2d91be
Revises: 5b3ce57c90d1
Create Date: 2026-10-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a8f4c2d91be"
down_revision: str | None = "5b3ce57c90d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("incident_evidence") as batch_op:
        batch_op.add_column(sa.Column("service", sa.String(length=200)))
        batch_op.add_column(sa.Column("normalized_summary", sa.Text()))
        batch_op.add_column(sa.Column("confidence", sa.Float()))
        batch_op.create_unique_constraint(
            "uq_evidence_incident_content_hash", ["tenant_id", "incident_id", "content_hash"]
        )
    op.execute("UPDATE incident_evidence SET service = 'unknown' WHERE service IS NULL")
    op.execute(
        "UPDATE incident_evidence SET normalized_summary = 'Legacy evidence without a summary' "
        "WHERE normalized_summary IS NULL"
    )
    op.execute("UPDATE incident_evidence SET confidence = 0 WHERE confidence IS NULL")
    with op.batch_alter_table("incident_evidence") as batch_op:
        batch_op.alter_column("service", existing_type=sa.String(length=200), nullable=False)
        batch_op.alter_column("normalized_summary", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("confidence", existing_type=sa.Float(), nullable=False)
    with op.batch_alter_table("incident_timeline") as batch_op:
        batch_op.add_column(sa.Column("evidence_id", sa.Uuid(), nullable=True))
        batch_op.create_unique_constraint("uq_incident_timeline_evidence", ["evidence_id"])
        batch_op.create_foreign_key(
            "fk_incident_timeline_evidence_id_incident_evidence",
            "incident_evidence",
            ["evidence_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("incident_timeline") as batch_op:
        batch_op.drop_constraint(
            "fk_incident_timeline_evidence_id_incident_evidence", type_="foreignkey"
        )
        batch_op.drop_constraint("uq_incident_timeline_evidence", type_="unique")
        batch_op.drop_column("evidence_id")
    with op.batch_alter_table("incident_evidence") as batch_op:
        batch_op.drop_constraint("uq_evidence_incident_content_hash", type_="unique")
        batch_op.drop_column("confidence")
        batch_op.drop_column("normalized_summary")
        batch_op.drop_column("service")
