"""邀请码与兑换记录模型。

表：`invite_codes`、`invite_redemptions`
唯一来源：`docs/architecture/02-database-design.md` 第 3.1 节。

并发兑换时由 Service 锁定邀请码行并在同一事务递增 `redeemed_count`。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

__all__ = ["InviteCode", "InviteRedemption"]


class InviteCode(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """`invite_codes`：只保存哈希，禁止保存明文邀请码。"""

    __tablename__ = "invite_codes"

    code_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    initial_points: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_redemptions: Mapped[int] = mapped_column(Integer, nullable=False)
    redeemed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'USED', 'EXPIRED', 'DISABLED')", name="status"
        ),
        CheckConstraint("initial_points > 0", name="initial_points_positive"),
        CheckConstraint("max_redemptions > 0", name="max_redemptions_positive"),
        CheckConstraint("redeemed_count >= 0", name="redeemed_count_non_negative"),
        CheckConstraint(
            "redeemed_count <= max_redemptions", name="redeemed_within_max_redemptions"
        ),
        Index("ix_invite_codes_status_expires_at", "status", "expires_at"),
    )


class InviteRedemption(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """`invite_redemptions`：兑换事实；与 `point_ledger` 的 CREDIT 同事务创建。"""

    __tablename__ = "invite_redemptions"

    invite_code_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("invite_codes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    points_credited: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        CheckConstraint("points_credited > 0", name="points_credited_positive"),
        UniqueConstraint(
            "invite_code_id", "user_id", name="uq_invite_redemptions_invite_code_user"
        ),
        Index("ix_invite_redemptions_user_id_created_at", "user_id", "created_at"),
    )
