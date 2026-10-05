"""0015_audit_logs：audit_logs。

对应 02-database-design.md 第 3.4 节与第 7 节顺序 15。
`entity_id` 是多态字段，不建立外键；本表只允许 INSERT，保护由 0016 建立。

Revision ID: 0015_audit_logs
Revises: 0014_shares_feedback
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_audit_logs"
down_revision: str | None = "0014_shares_feedback"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=96), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
        sa.CheckConstraint(
            "actor_type IN ('USER', 'ADMIN', 'SYSTEM', 'WORKER')",
            name=op.f("ck_audit_logs_actor_type"),
        ),
    )
    op.create_index(
        op.f("ix_audit_logs_entity_type_entity_id_created_at"),
        "audit_logs",
        ["entity_type", "entity_id", "created_at"],
    )
    op.create_index(
        op.f("ix_audit_logs_actor_id_created_at"),
        "audit_logs",
        ["actor_id", "created_at"],
        postgresql_where=sa.text("actor_id IS NOT NULL"),
    )
    op.create_index(
        op.f("ix_audit_logs_request_id"),
        "audit_logs",
        ["request_id"],
        postgresql_where=sa.text("request_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_logs_request_id"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_actor_id_created_at"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_entity_type_entity_id_created_at"), table_name="audit_logs")
    op.drop_table("audit_logs")
