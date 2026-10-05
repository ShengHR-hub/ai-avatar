"""用户与邮箱身份模型。

表：`users`、`user_emails`
唯一来源：`docs/architecture/02-database-design.md` 第 3.1 节。

注意：`users.avatar_asset_id` 指向 `media_assets.id`，与 `media_assets.user_id`
构成外键循环，因此该外键使用 `use_alter=True`，由迁移 `0006_media_reference_fks` 添加。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin

__all__ = ["User", "UserEmail"]


class User(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """`users`：用户身份、本地化设置与生命周期状态。"""

    __tablename__ = "users"

    display_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    avatar_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'DELETED')", name="status"),
        Index("ix_users_status_created_at", "status", "created_at"),
    )


class UserEmail(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """`user_emails`：规范化邮箱；令牌与 Session 只存 Redis。"""

    __tablename__ = "user_emails"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )

    __table_args__ = (
        Index(
            "uq_user_emails_user_id_primary",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )
