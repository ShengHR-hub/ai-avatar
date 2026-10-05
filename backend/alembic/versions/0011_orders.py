"""0011_orders：orders。

对应 02-database-design.md 第 3.4 节与第 7 节顺序 11。
V1.0 只保留订单结构，不实现支付网关或价格规则。

Revision ID: 0011_orders
Revises: 0010_generation_io
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011_orders"
down_revision: str | None = "0010_generation_io"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("points_amount", sa.BigInteger(), nullable=False),
        sa.Column("provider_order_id", sa.String(length=256), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_orders_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name=op.f("uq_orders_user_idempotency")
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PAID', 'CANCELLED', 'REFUNDED')",
            name=op.f("ck_orders_status"),
        ),
        sa.CheckConstraint(
            "amount_minor >= 0", name=op.f("ck_orders_amount_minor_non_negative")
        ),
        sa.CheckConstraint("points_amount > 0", name=op.f("ck_orders_points_amount_positive")),
    )
    op.create_index(
        op.f("ix_orders_user_id_created_at_id"), "orders", ["user_id", "created_at", "id"]
    )
    op.create_index(
        op.f("uq_orders_provider_order_id"),
        "orders",
        ["provider_order_id"],
        unique=True,
        postgresql_where=sa.text("provider_order_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(op.f("uq_orders_provider_order_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_user_id_created_at_id"), table_name="orders")
    op.drop_table("orders")
