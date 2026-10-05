"""0013_point_ledger：point_ledger。

对应 02-database-design.md 第 3.2 节与第 7 节顺序 13。

- 本表只允许 INSERT，是点数事实来源；UPDATE/DELETE 保护由 0016 建立；
- CHECK 固定 CREDIT / RESERVE / CONSUME / RELEASE 四种标准 delta 变化，
  ADJUSTMENT 要求至少一个 delta 非零；
- 两个部分唯一索引保证：每个任务最多一次 RESERVE，且 CONSUME 与 RELEASE 互斥、总共最多一次。

Revision ID: 0013_point_ledger
Revises: 0012_provider_attempts
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_point_ledger"
down_revision: str | None = "0012_provider_attempts"
branch_labels: str | None = None
depends_on: str | None = None

_ENTRY_TYPE_DELTA_CHECK = (
    "(entry_type = 'CREDIT' AND available_delta = amount AND reserved_delta = 0) "
    "OR (entry_type = 'RESERVE' AND available_delta = -amount AND reserved_delta = amount) "
    "OR (entry_type = 'CONSUME' AND available_delta = 0 AND reserved_delta = -amount) "
    "OR (entry_type = 'RELEASE' AND available_delta = amount AND reserved_delta = -amount) "
    "OR (entry_type = 'ADJUSTMENT' AND (available_delta <> 0 OR reserved_delta <> 0))"
)


def upgrade() -> None:
    op.create_table(
        "point_ledger",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("wallet_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("entry_type", sa.String(length=16), nullable=False),
        sa.Column("available_delta", sa.BigInteger(), nullable=False),
        sa.Column("reserved_delta", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("generation_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("order_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["point_wallets.id"],
            name=op.f("fk_point_ledger_wallet_id_point_wallets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_point_ledger_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["generation_tasks.id"],
            name=op.f("fk_point_ledger_generation_id_generation_tasks"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_point_ledger_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_point_ledger")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_point_ledger_idempotency_key")
        ),
        sa.CheckConstraint(
            "entry_type IN ('CREDIT', 'RESERVE', 'CONSUME', 'RELEASE', 'ADJUSTMENT')",
            name=op.f("ck_point_ledger_entry_type"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_point_ledger_amount_positive")),
        sa.CheckConstraint(
            _ENTRY_TYPE_DELTA_CHECK, name=op.f("ck_point_ledger_entry_type_delta")
        ),
    )
    op.create_index(
        op.f("ix_point_ledger_user_id_created_at_id"),
        "point_ledger",
        ["user_id", "created_at", "id"],
    )
    op.create_index(
        op.f("ix_point_ledger_wallet_id_created_at_id"),
        "point_ledger",
        ["wallet_id", "created_at", "id"],
    )
    op.create_index(
        op.f("ix_point_ledger_generation_id"),
        "point_ledger",
        ["generation_id"],
        postgresql_where=sa.text("generation_id IS NOT NULL"),
    )
    # 一个任务最多一次预留
    op.create_index(
        op.f("uq_point_ledger_generation_reserve"),
        "point_ledger",
        ["generation_id"],
        unique=True,
        postgresql_where=sa.text("entry_type = 'RESERVE'"),
    )
    # 一个任务最多一次最终结算，且 CONSUME 与 RELEASE 互斥
    op.create_index(
        op.f("uq_point_ledger_generation_final"),
        "point_ledger",
        ["generation_id"],
        unique=True,
        postgresql_where=sa.text("entry_type IN ('CONSUME', 'RELEASE')"),
    )


def downgrade() -> None:
    op.drop_index(op.f("uq_point_ledger_generation_final"), table_name="point_ledger")
    op.drop_index(op.f("uq_point_ledger_generation_reserve"), table_name="point_ledger")
    op.drop_index(op.f("ix_point_ledger_generation_id"), table_name="point_ledger")
    op.drop_index(op.f("ix_point_ledger_wallet_id_created_at_id"), table_name="point_ledger")
    op.drop_index(op.f("ix_point_ledger_user_id_created_at_id"), table_name="point_ledger")
    op.drop_table("point_ledger")
