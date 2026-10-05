"""0002_users_identity：users、user_emails。

对应 02-database-design.md 第 3.1 节与第 7 节顺序 2。
`users.avatar_asset_id` 此处只建列，外键由 0006 添加（避免与 media_assets 建表循环）。

Revision ID: 0002_users_identity
Revises: 0001_v1_extensions
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_users_identity"
down_revision: str | None = "0001_v1_extensions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("display_name", sa.String(length=80), nullable=True),
        sa.Column("avatar_asset_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'DELETED')", name=op.f("ck_users_status")
        ),
    )
    op.create_index(op.f("ix_users_status_created_at"), "users", ["status", "created_at"])

    op.create_table(
        "user_emails",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_emails_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_emails")),
        sa.UniqueConstraint("email", name=op.f("uq_user_emails_email")),
    )
    # 每个用户最多一个主邮箱
    op.create_index(
        op.f("uq_user_emails_user_id_primary"),
        "user_emails",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )


def downgrade() -> None:
    op.drop_index(op.f("uq_user_emails_user_id_primary"), table_name="user_emails")
    op.drop_table("user_emails")
    op.drop_index(op.f("ix_users_status_created_at"), table_name="users")
    op.drop_table("users")
