"""0005_media_assets：media_assets。

对应 02-database-design.md 第 3.3 节与第 7 节顺序 5。
只保存对象存储引用与生命周期信息，不保存图片二进制。

Revision ID: 0005_media_assets
Revises: 0004_wallets
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_media_assets"
down_revision: str | None = "0004_wallets"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("asset_type", sa.String(length=16), nullable=False),
        sa.Column(
            "visibility",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'PRIVATE'"),
        ),
        sa.Column("storage_provider", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_media_assets_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_assets")),
        sa.UniqueConstraint(
            "storage_provider",
            "storage_key",
            name=op.f("uq_media_assets_storage_provider_storage_key"),
        ),
        sa.CheckConstraint(
            "asset_type IN ('ORIGINAL', 'REFERENCE', 'RESULT')",
            name=op.f("ck_media_assets_asset_type"),
        ),
        sa.CheckConstraint(
            "visibility IN ('PRIVATE', 'SHARED', 'PUBLIC')",
            name=op.f("ck_media_assets_visibility"),
        ),
        sa.CheckConstraint(
            "storage_provider IN ('LOCAL', 'MINIO', 'COS', 'OSS', 'S3')",
            name=op.f("ck_media_assets_storage_provider"),
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_media_assets_size_bytes_positive")),
        sa.CheckConstraint("width > 0", name=op.f("ck_media_assets_width_positive")),
        sa.CheckConstraint("height > 0", name=op.f("ck_media_assets_height_positive")),
    )
    op.create_index(
        op.f("ix_media_assets_user_id_created_at_id"), "media_assets", ["user_id", "created_at", "id"]
    )
    # 生命周期清理只针对未逻辑删除且已设置过期时间的资源
    op.create_index(
        op.f("ix_media_assets_expires_at"),
        "media_assets",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_media_assets_expires_at"), table_name="media_assets")
    op.drop_index(op.f("ix_media_assets_user_id_created_at_id"), table_name="media_assets")
    op.drop_table("media_assets")
