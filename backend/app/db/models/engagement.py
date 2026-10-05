"""分享与反馈模型。

表：`shares`、`share_assets`、`feedback`
唯一来源：`docs/architecture/02-database-design.md` 第 3.4 节与第 3.9 节。

创建分享必须同时把目标媒体 `visibility` 置为 `SHARED`；撤销/过期只改变状态和
可见性，不删除任务历史。

ENG-09（迁移 0021）的两处变更：

- `shares.asset_id` **保留**，语义收紧为「选中输出中 `output_index` 最小的主资产」，
  保证该列在 `NOT NULL` 下依然成立，也让历史单图分享无损可读；
- 新增 `share_assets` 承载**完整**的选中资产集合（一条链接可以分享一张或两张结果图）。
  匿名读取优先取关联表，关联为空时回落到 `asset_id`（历史兼容），因此旧数据不需要
  任何改写就能继续被正确读取；
- `feedback.category` 扩宽到 `varchar(64)`，与冻结的 `FeedbackCreateRequest.category`
  （maxLength 64）一致；请求字段与长度契约本身没有变化。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin

__all__ = ["Feedback", "Share", "ShareAsset"]


class Share(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """`shares`：公开分享链接及其状态。"""

    __tablename__ = "shares"

    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED', 'EXPIRED')", name="status"
        ),
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_shares_user_idempotency"
        ),
        Index("ix_shares_generation_id_status", "generation_id", "status"),
    )


class ShareAsset(Base):
    """`share_assets`：一次分享**选中**的结果资产集合（迁移 0021）。

    复合主键 `(share_id, asset_id)` 让「同一次分享里同一张图只能关联一次」由数据库
    裁决；`sort_order` 取自 `generation_outputs.output_index`，决定分享页的呈现顺序。

    这张表**只登记被明确选中的资产**：同任务未选中的结果不会被写入，也不会被置为
    `SHARED`，因此不会因为别人分享了两张中的一张而把另一张也暴露出去。
    """

    __tablename__ = "share_assets"

    share_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("shares.id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        # 原始名交给命名约定渲染成 `ck_share_assets_sort_order`
        # （不要再手写带表前缀的全名，否则会得到双前缀）
        CheckConstraint("sort_order >= 0", name="sort_order"),
        UniqueConstraint(
            "share_id", "sort_order", name="uq_share_assets_share_id_sort_order"
        ),
        # 生命周期按资产反查「还有没有有效分享」时需要；名字与 0021 的显式 DDL 一致。
        Index("ix_share_assets_asset_id_share_id", "asset_id", "share_id"),
    )


class Feedback(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """`feedback`：一个用户对同一任务只有一条 V1.0 反馈。"""

    __tablename__ = "feedback"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # 冻结契约 `FeedbackCreateRequest.category` 是 maxLength 64（迁移 0021 扩宽）
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="OPEN", default="OPEN"
    )

    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),
        CheckConstraint(
            "status IN ('OPEN', 'REVIEWED', 'RESOLVED')", name="status"
        ),
        UniqueConstraint(
            "user_id", "generation_id", name="uq_feedback_user_generation"
        ),
        Index("ix_feedback_status_created_at", "status", "created_at"),
    )
