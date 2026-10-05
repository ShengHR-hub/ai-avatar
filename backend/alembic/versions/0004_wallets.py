"""0004_wallets：point_wallets。

对应 02-database-design.md 第 3.2 节与第 7 节顺序 4。
钱包只是与流水同事务更新的余额快照；四个余额字段与 version 均非负。

Revision ID: 0004_wallets
Revises: 0003_invites
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_wallets"
down_revision: str | None = "0003_invites"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "point_wallets",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "available_points",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "reserved_points",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "lifetime_credited",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "lifetime_consumed",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
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
            name=op.f("fk_point_wallets_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_point_wallets")),
        sa.UniqueConstraint("user_id", name=op.f("uq_point_wallets_user_id")),
        sa.CheckConstraint(
            "available_points >= 0", name=op.f("ck_point_wallets_available_points_non_negative")
        ),
        sa.CheckConstraint(
            "reserved_points >= 0", name=op.f("ck_point_wallets_reserved_points_non_negative")
        ),
        sa.CheckConstraint(
            "lifetime_credited >= 0", name=op.f("ck_point_wallets_lifetime_credited_non_negative")
        ),
        sa.CheckConstraint(
            "lifetime_consumed >= 0", name=op.f("ck_point_wallets_lifetime_consumed_non_negative")
        ),
        sa.CheckConstraint("version >= 0", name=op.f("ck_point_wallets_version_non_negative")),
    )


def downgrade() -> None:
    op.drop_table("point_wallets")
