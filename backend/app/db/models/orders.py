"""订单预留模型。

表：`orders`
唯一来源：`docs/architecture/02-database-design.md` 第 3.4 节。

V1.0 只保留结构，不实现支付网关或价格规则；未来支付回调必须通过外部订单号
和幂等键处理。
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
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin

__all__ = ["Order"]


class Order(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """`orders`：订单预留与幂等键。"""

    __tablename__ = "orders"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    points_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'PAID', 'CANCELLED', 'REFUNDED')", name="status"
        ),
        CheckConstraint("amount_minor >= 0", name="amount_minor_non_negative"),
        CheckConstraint("points_amount > 0", name="points_amount_positive"),
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_orders_user_idempotency"
        ),
        Index("ix_orders_user_id_created_at_id", "user_id", "created_at", "id"),
        Index(
            "uq_orders_provider_order_id",
            "provider_order_id",
            unique=True,
            postgresql_where=text("provider_order_id IS NOT NULL"),
        ),
    )
