"""0008_generation_cost_configs：generation_cost_configs。

对应 02-database-design.md 第 3.3 节与第 7 节顺序 8。

唯一键 `(mode, model_name, output_count, effective_from)` 使用
`UNIQUE NULLS NOT DISTINCT`（PostgreSQL 15+）：`model_name` 可空，
默认的 NULL 互不相等语义会允许重复写入同一配置窗口。

Revision ID: 0008_generation_cost_configs
Revises: 0007_styles_and_templates
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008_generation_cost_configs"
down_revision: str | None = "0007_styles_and_templates"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "generation_cost_configs",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("output_count", sa.Integer(), nullable=False),
        sa.Column("points_cost", sa.BigInteger(), nullable=False),
        sa.Column(
            "cost_multiplier",
            sa.Numeric(precision=12, scale=4),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_cost_configs")),
        sa.UniqueConstraint(
            "mode",
            "model_name",
            "output_count",
            "effective_from",
            name=op.f("uq_generation_cost_configs_window"),
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')", name=op.f("ck_generation_cost_configs_status")
        ),
        sa.CheckConstraint(
            "output_count > 0", name=op.f("ck_generation_cost_configs_output_count_positive")
        ),
        sa.CheckConstraint(
            "points_cost > 0", name=op.f("ck_generation_cost_configs_points_cost_positive")
        ),
        sa.CheckConstraint(
            "cost_multiplier > 0",
            name=op.f("ck_generation_cost_configs_cost_multiplier_positive"),
        ),
        sa.CheckConstraint(
            "effective_to > effective_from",
            name=op.f("ck_generation_cost_configs_effective_window"),
        ),
    )
    op.create_index(
        op.f("ix_generation_cost_configs_status_effective_from"),
        "generation_cost_configs",
        ["status", "effective_from"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_generation_cost_configs_status_effective_from"),
        table_name="generation_cost_configs",
    )
    op.drop_table("generation_cost_configs")
