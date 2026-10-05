"""媒体资源元数据模型。

表：`media_assets`
唯一来源：`docs/architecture/02-database-design.md` 第 3.3 节。

PostgreSQL 只保存对象存储引用和生命周期信息，不保存图片二进制；
`deleted_at` 为逻辑删除，二进制清理由生命周期任务处理，不级联删除业务历史。

ENG-05（迁移 0019）新增三列：

- `purpose`：上传用途 `SOURCE/REFERENCE/RESULT`，与 `asset_type` 的映射固定为
  `SOURCE -> ORIGINAL`、`REFERENCE -> REFERENCE`、`RESULT -> RESULT`（`asset_type` 语义不变）；
- `status`：生命周期状态 `PENDING_UPLOAD/READY/DELETED/EXPIRED`；
- `idempotency_key`：上传意图的**操作级**幂等键，可空（结果图由 ENG-06 生成时无上传请求键），
  由唯一部分索引 `(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL` 兜底防重。

状态与时间戳的一致性由数据库 CHECK 保证 `status = 'DELETED'` 与 `deleted_at IS NOT NULL`
互为充要条件；`EXPIRED` 由生命周期任务在 `expires_at` 到期后写入，因此数据库只约束其
必要前提（`status = 'EXPIRED'` 必须存在 `expires_at`），不把墙上时钟写进 CHECK。
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

__all__ = [
    "ASSET_PURPOSES",
    "ASSET_STATUSES",
    "ASSET_TYPES",
    "MediaAsset",
    "VISIBILITIES",
]

#: 以下元组逐字对应本表 `__table_args__` 中的数据库 CHECK（数据库是唯一权威，
#: 这里只是让 Repository/Service 引用同一份字面量，不各写一遍枚举）。
ASSET_TYPES: tuple[str, ...] = ("ORIGINAL", "REFERENCE", "RESULT")
ASSET_PURPOSES: tuple[str, ...] = ("SOURCE", "REFERENCE", "RESULT")
ASSET_STATUSES: tuple[str, ...] = ("PENDING_UPLOAD", "READY", "DELETED", "EXPIRED")
VISIBILITIES: tuple[str, ...] = ("PRIVATE", "SHARED", "PUBLIC")


class MediaAsset(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """`media_assets`：原图、参考图与结果图的元数据。"""

    __tablename__ = "media_assets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="PRIVATE", default="PRIVATE"
    )
    storage_provider: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "asset_type IN ('ORIGINAL', 'REFERENCE', 'RESULT')", name="asset_type"
        ),
        CheckConstraint(
            "purpose IN ('SOURCE', 'REFERENCE', 'RESULT')", name="purpose"
        ),
        CheckConstraint(
            "status IN ('PENDING_UPLOAD', 'READY', 'DELETED', 'EXPIRED')", name="status"
        ),
        # 逻辑删除与状态互为充要条件：不允许「有 deleted_at 但状态不是 DELETED」，
        # 也不允许「标了 DELETED 却没有时间戳」。
        CheckConstraint(
            "(deleted_at IS NOT NULL) = (status = 'DELETED')",
            name="deleted_at_matches_status",
        ),
        # EXPIRED 的必要前提（到期时刻本身由生命周期任务与 now() 比较，不写进 CHECK）
        CheckConstraint(
            "status <> 'EXPIRED' OR expires_at IS NOT NULL",
            name="expired_requires_expires_at",
        ),
        CheckConstraint(
            "visibility IN ('PRIVATE', 'SHARED', 'PUBLIC')", name="visibility"
        ),
        CheckConstraint(
            "storage_provider IN ('LOCAL', 'MINIO', 'COS', 'OSS', 'S3')",
            name="storage_provider",
        ),
        CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
        CheckConstraint("width > 0", name="width_positive"),
        CheckConstraint("height > 0", name="height_positive"),
        UniqueConstraint(
            "storage_provider",
            "storage_key",
            name="uq_media_assets_storage_provider_storage_key",
        ),
        # 上传幂等的数据库兜底：同一用户的同一操作级键只能有一条意图行。
        # 只覆盖带键的行（结果图等无键资源不受影响），因此是部分唯一索引而非表级唯一键。
        Index(
            "uq_media_assets_user_id_idempotency_key",
            "user_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_media_assets_user_id_created_at_id", "user_id", "created_at", "id"),
        Index(
            "ix_media_assets_expires_at",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL AND deleted_at IS NULL"),
        ),
    )
