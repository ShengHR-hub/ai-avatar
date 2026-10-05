"""0010_generation_io：generation_inputs、generation_outputs。

对应 02-database-design.md 第 3.3 节与第 7 节顺序 10。
输出序号在任务内唯一；Provider 返回的结果 ID 在同一任务内也不得重复。

Revision ID: 0010_generation_io
Revises: 0009_generation_tasks
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010_generation_io"
down_revision: str | None = "0009_generation_tasks"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "generation_inputs",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("generation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("asset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["generation_tasks.id"],
            name=op.f("fk_generation_inputs_generation_id_generation_tasks"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["media_assets.id"],
            name=op.f("fk_generation_inputs_asset_id_media_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_inputs")),
        sa.UniqueConstraint(
            "generation_id",
            "role",
            "sort_order",
            name=op.f("uq_generation_inputs_generation_role_sort"),
        ),
        sa.UniqueConstraint(
            "generation_id",
            "asset_id",
            name=op.f("uq_generation_inputs_generation_asset"),
        ),
        sa.CheckConstraint(
            "role IN ('SOURCE', 'REFERENCE')", name=op.f("ck_generation_inputs_role")
        ),
        sa.CheckConstraint(
            "sort_order >= 0", name=op.f("ck_generation_inputs_sort_order_non_negative")
        ),
    )
    op.create_index(
        op.f("ix_generation_inputs_generation_id_role_sort_order"),
        "generation_inputs",
        ["generation_id", "role", "sort_order"],
    )

    op.create_table(
        "generation_outputs",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("generation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("asset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("output_index", sa.Integer(), nullable=False),
        sa.Column("provider_output_id", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["generation_tasks.id"],
            name=op.f("fk_generation_outputs_generation_id_generation_tasks"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["media_assets.id"],
            name=op.f("fk_generation_outputs_asset_id_media_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_outputs")),
        sa.UniqueConstraint(
            "generation_id",
            "output_index",
            name=op.f("uq_generation_outputs_generation_output_index"),
        ),
        sa.CheckConstraint(
            "output_index >= 0", name=op.f("ck_generation_outputs_output_index_non_negative")
        ),
    )
    op.create_index(
        op.f("ix_generation_outputs_generation_id_output_index"),
        "generation_outputs",
        ["generation_id", "output_index"],
    )
    # Provider 返回结果 ID 时，同一任务内不得重复
    op.create_index(
        op.f("uq_generation_outputs_generation_provider_output"),
        "generation_outputs",
        ["generation_id", "provider_output_id"],
        unique=True,
        postgresql_where=sa.text("provider_output_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        op.f("uq_generation_outputs_generation_provider_output"),
        table_name="generation_outputs",
    )
    op.drop_index(
        op.f("ix_generation_outputs_generation_id_output_index"),
        table_name="generation_outputs",
    )
    op.drop_table("generation_outputs")
    op.drop_index(
        op.f("ix_generation_inputs_generation_id_role_sort_order"),
        table_name="generation_inputs",
    )
    op.drop_table("generation_inputs")
