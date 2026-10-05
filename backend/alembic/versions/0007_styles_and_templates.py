"""0007_styles_and_templates：styles、prompt_templates。

对应 02-database-design.md 第 3.4 节与第 7 节顺序 7。
发布版本唯一性由 (slug, mode) WHERE status='PUBLISHED' 部分唯一索引保证。

Revision ID: 0007_styles_and_templates
Revises: 0006_media_reference_fks
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007_styles_and_templates"
down_revision: str | None = "0006_media_reference_fks"
branch_labels: str | None = None
depends_on: str | None = None

_PUBLICATION_STATUS = "status IN ('DRAFT', 'PUBLISHED', 'ARCHIVED')"


def upgrade() -> None:
    op.create_table(
        "styles",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slug", sa.String(length=96), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("cover_asset_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["cover_asset_id"],
            ["media_assets.id"],
            name=op.f("fk_styles_cover_asset_id_media_assets"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_styles")),
        sa.UniqueConstraint("slug", name=op.f("uq_styles_slug")),
        sa.CheckConstraint(_PUBLICATION_STATUS, name=op.f("ck_styles_status")),
    )
    op.create_index(op.f("ix_styles_status_sort_order"), "styles", ["status", "sort_order"])

    op.create_table(
        "prompt_templates",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("style_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("slug", sa.String(length=96), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["style_id"],
            ["styles.id"],
            name=op.f("fk_prompt_templates_style_id_styles"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_templates")),
        sa.UniqueConstraint(
            "slug", "mode", "version", name=op.f("uq_prompt_templates_slug_mode_version")
        ),
        sa.CheckConstraint(_PUBLICATION_STATUS, name=op.f("ck_prompt_templates_status")),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_prompt_templates_version_positive")
        ),
    )
    # 每个模式最多一个当前发布版本
    op.create_index(
        op.f("uq_prompt_templates_slug_mode_published"),
        "prompt_templates",
        ["slug", "mode"],
        unique=True,
        postgresql_where=sa.text("status = 'PUBLISHED'"),
    )
    op.create_index(
        op.f("ix_prompt_templates_style_id_status"),
        "prompt_templates",
        ["style_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_prompt_templates_style_id_status"), table_name="prompt_templates")
    op.drop_index(
        op.f("uq_prompt_templates_slug_mode_published"), table_name="prompt_templates"
    )
    op.drop_table("prompt_templates")
    op.drop_index(op.f("ix_styles_status_sort_order"), table_name="styles")
    op.drop_table("styles")
