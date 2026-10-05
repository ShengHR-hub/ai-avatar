"""0012_provider_attempts：provider_attempts。

对应 02-database-design.md 第 3.3 节与第 7 节顺序 12。
记录每次 Provider 调用的状态、用量与估算成本；图片模型 Token 字段可为空。
`estimated_cost` 存在时必须填写 `currency`。

Revision ID: 0012_provider_attempts
Revises: 0011_orders
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_provider_attempts"
down_revision: str | None = "0011_orders"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "provider_attempts",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("generation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("image_count", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column(
            "request_metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "response_metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["generation_tasks.id"],
            name=op.f("fk_provider_attempts_generation_id_generation_tasks"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_attempts")),
        sa.UniqueConstraint(
            "generation_id",
            "attempt_no",
            name=op.f("uq_provider_attempts_generation_attempt"),
        ),
        sa.CheckConstraint(
            "status IN ('STARTED', 'SUBMITTED', 'SUCCEEDED', 'FAILED', "
            "'TIMEOUT', 'UNKNOWN')",
            name=op.f("ck_provider_attempts_status"),
        ),
        sa.CheckConstraint("attempt_no > 0", name=op.f("ck_provider_attempts_attempt_no_positive")),
        sa.CheckConstraint(
            "input_tokens >= 0", name=op.f("ck_provider_attempts_input_tokens_non_negative")
        ),
        sa.CheckConstraint(
            "output_tokens >= 0", name=op.f("ck_provider_attempts_output_tokens_non_negative")
        ),
        sa.CheckConstraint(
            "image_count >= 0", name=op.f("ck_provider_attempts_image_count_non_negative")
        ),
        sa.CheckConstraint(
            "estimated_cost >= 0",
            name=op.f("ck_provider_attempts_estimated_cost_non_negative"),
        ),
        # 有成本时必须给出币种
        sa.CheckConstraint(
            "estimated_cost IS NULL OR currency IS NOT NULL",
            name=op.f("ck_provider_attempts_currency_required_with_cost"),
        ),
    )
    op.create_index(
        op.f("ix_provider_attempts_generation_id_attempt_no"),
        "provider_attempts",
        ["generation_id", "attempt_no"],
    )
    # 同一外部请求不得被重复记账
    op.create_index(
        op.f("uq_provider_attempts_provider_request"),
        "provider_attempts",
        ["provider_name", "request_id"],
        unique=True,
        postgresql_where=sa.text("request_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        op.f("uq_provider_attempts_provider_request"), table_name="provider_attempts"
    )
    op.drop_index(
        op.f("ix_provider_attempts_generation_id_attempt_no"), table_name="provider_attempts"
    )
    op.drop_table("provider_attempts")
