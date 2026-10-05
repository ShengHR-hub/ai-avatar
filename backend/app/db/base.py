"""SQLAlchemy 2.x 声明式基类与公共列（ENG-01 第 1 步）。

唯一来源：`docs/architecture/02-database-design.md`
第 1 节（冻结原则）与第 3 节（表定义）。

约定：

- 主键统一 UUID，数据库默认 `gen_random_uuid()`，应用也可显式传入 UUID；
- 时间统一 `timestamptz`（`DateTime(timezone=True)`），应用只写 UTC；
- 状态使用 `varchar + CheckConstraint`，不使用 PostgreSQL enum；
- 统一命名约定，保证模型与手写迁移的约束名一致。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, MetaData, Uuid, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Base",
    "CreatedAtMixin",
    "NAMING_CONVENTION",
    "UpdatedAtMixin",
    "UUIDPrimaryKeyMixin",
    "utcnow",
]

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """返回带 UTC 时区的当前时间；应用只写 UTC。"""

    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """所有 V1.0 模型的声明式基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        uuid.UUID: Uuid(as_uuid=True),
        datetime: DateTime(timezone=True),
    }


class UUIDPrimaryKeyMixin:
    """UUID 主键：数据库默认 `gen_random_uuid()`，应用可显式传入。"""

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        nullable=False,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    )


class CreatedAtMixin:
    """`created_at`：`timestamptz NOT NULL DEFAULT now()`。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        default=utcnow,
    )


class UpdatedAtMixin:
    """`updated_at`：`timestamptz NOT NULL`。

    设计文档只规定 `NOT NULL`，未规定数据库默认值，因此由应用写入，
    不额外引入数据库默认值。
    """

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )
