"""0009_generation_tasks：generation_tasks。

对应 02-database-design.md 第 3.3 节与第 7 节顺序 9。
任务状态与计费状态分离；重试通过自引用 `parent_task_id` 指向历史任务。

Revision ID: 0009_generation_tasks
Revises: 0008_generation_cost_configs
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_generation_tasks"
down_revision: str | None = "0008_generation_cost_configs"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "generation_tasks",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("parent_task_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("billing_status", sa.String(length=16), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("style_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("prompt_template_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("cost_config_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("cost_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("reserved_points", sa.BigInteger(), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
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
            name=op.f("fk_generation_tasks_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_task_id"],
            ["generation_tasks.id"],
            name=op.f("fk_generation_tasks_parent_task_id_generation_tasks"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["style_id"],
            ["styles.id"],
            name=op.f("fk_generation_tasks_style_id_styles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_template_id"],
            ["prompt_templates.id"],
            name=op.f("fk_generation_tasks_prompt_template_id_prompt_templates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cost_config_id"],
            ["generation_cost_configs.id"],
            name=op.f("fk_generation_tasks_cost_config_id_generation_cost_configs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_tasks")),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name=op.f("uq_generation_tasks_user_idempotency")
        ),
        sa.CheckConstraint(
            "mode IN ('PHOTO_PROMPT', 'TEXT_PROMPT', 'REFERENCE_IMAGE')",
            name=op.f("ck_generation_tasks_mode"),
        ),
        sa.CheckConstraint(
            "status IN ('CREATED', 'QUEUED', 'PROCESSING', 'SUCCEEDED', "
            "'FAILED', 'CANCELLED', 'NEEDS_REVIEW')",
            name=op.f("ck_generation_tasks_status"),
        ),
        sa.CheckConstraint(
            "billing_status IN ('RESERVED', 'CONSUMED', 'RELEASED')",
            name=op.f("ck_generation_tasks_billing_status"),
        ),
        sa.CheckConstraint(
            "requested_count > 0", name=op.f("ck_generation_tasks_requested_count_positive")
        ),
        sa.CheckConstraint(
            "reserved_points > 0", name=op.f("ck_generation_tasks_reserved_points_positive")
        ),
        sa.CheckConstraint(
            "retry_count >= 0", name=op.f("ck_generation_tasks_retry_count_non_negative")
        ),
    )
    op.create_index(
        op.f("ix_generation_tasks_user_id_created_at_id"),
        "generation_tasks",
        ["user_id", "created_at", "id"],
    )
    op.create_index(
        op.f("ix_generation_tasks_status_created_at_id"),
        "generation_tasks",
        ["status", "created_at", "id"],
    )
    op.create_index(
        op.f("ix_generation_tasks_parent_task_id"),
        "generation_tasks",
        ["parent_task_id"],
        postgresql_where=sa.text("parent_task_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_generation_tasks_parent_task_id"), table_name="generation_tasks")
    op.drop_index(
        op.f("ix_generation_tasks_status_created_at_id"), table_name="generation_tasks"
    )
    op.drop_index(
        op.f("ix_generation_tasks_user_id_created_at_id"), table_name="generation_tasks"
    )
    op.drop_table("generation_tasks")
