"""0014_shares_feedback：shares、feedback。

对应 02-database-design.md 第 3.4 节与第 7 节顺序 14。
分享只暴露显式标记为 SHARED 的资源；一个用户对同一任务只有一条反馈。

Revision ID: 0014_shares_feedback
Revises: 0013_point_ledger
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014_shares_feedback"
down_revision: str | None = "0013_point_ledger"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "shares",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("generation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("asset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_shares_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["generation_tasks.id"],
            name=op.f("fk_shares_generation_id_generation_tasks"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["media_assets.id"],
            name=op.f("fk_shares_asset_id_media_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shares")),
        sa.UniqueConstraint("slug", name=op.f("uq_shares_slug")),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name=op.f("uq_shares_user_idempotency")
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED', 'EXPIRED')", name=op.f("ck_shares_status")
        ),
    )
    op.create_index(op.f("ix_shares_generation_id_status"), "shares", ["generation_id", "status"])

    op.create_table(
        "feedback",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("generation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'OPEN'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_feedback_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["generation_tasks.id"],
            name=op.f("fk_feedback_generation_id_generation_tasks"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
        sa.UniqueConstraint(
            "user_id", "generation_id", name=op.f("uq_feedback_user_generation")
        ),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name=op.f("ck_feedback_rating_range")),
        sa.CheckConstraint(
            "status IN ('OPEN', 'REVIEWED', 'RESOLVED')", name=op.f("ck_feedback_status")
        ),
    )
    op.create_index(op.f("ix_feedback_status_created_at"), "feedback", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_feedback_status_created_at"), table_name="feedback")
    op.drop_table("feedback")
    op.drop_index(op.f("ix_shares_generation_id_status"), table_name="shares")
    op.drop_table("shares")
