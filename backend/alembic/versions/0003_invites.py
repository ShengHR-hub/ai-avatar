"""0003_invites：invite_codes、invite_redemptions。

对应 02-database-design.md 第 3.1 节与第 7 节顺序 3。
邀请码只保存哈希；兑换事实由 (invite_code_id, user_id) 唯一键保护。

Revision ID: 0003_invites
Revises: 0002_users_identity
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003_invites"
down_revision: str | None = "0002_users_identity"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "invite_codes",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("initial_points", sa.BigInteger(), nullable=False),
        sa.Column("max_redemptions", sa.Integer(), nullable=False),
        sa.Column(
            "redeemed_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_invite_codes_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invite_codes")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_invite_codes_code_hash")),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'USED', 'EXPIRED', 'DISABLED')",
            name=op.f("ck_invite_codes_status"),
        ),
        sa.CheckConstraint(
            "initial_points > 0", name=op.f("ck_invite_codes_initial_points_positive")
        ),
        sa.CheckConstraint(
            "max_redemptions > 0", name=op.f("ck_invite_codes_max_redemptions_positive")
        ),
        sa.CheckConstraint(
            "redeemed_count >= 0", name=op.f("ck_invite_codes_redeemed_count_non_negative")
        ),
        sa.CheckConstraint(
            "redeemed_count <= max_redemptions",
            name=op.f("ck_invite_codes_redeemed_within_max_redemptions"),
        ),
    )
    op.create_index(
        op.f("ix_invite_codes_status_expires_at"), "invite_codes", ["status", "expires_at"]
    )

    op.create_table(
        "invite_redemptions",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("invite_code_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("points_credited", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["invite_code_id"],
            ["invite_codes.id"],
            name=op.f("fk_invite_redemptions_invite_code_id_invite_codes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_invite_redemptions_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invite_redemptions")),
        sa.UniqueConstraint(
            "invite_code_id",
            "user_id",
            name=op.f("uq_invite_redemptions_invite_code_user"),
        ),
        sa.CheckConstraint(
            "points_credited > 0",
            name=op.f("ck_invite_redemptions_points_credited_positive"),
        ),
    )
    op.create_index(
        op.f("ix_invite_redemptions_user_id_created_at"),
        "invite_redemptions",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_invite_redemptions_user_id_created_at"), table_name="invite_redemptions"
    )
    op.drop_table("invite_redemptions")
    op.drop_index(op.f("ix_invite_codes_status_expires_at"), table_name="invite_codes")
    op.drop_table("invite_codes")
