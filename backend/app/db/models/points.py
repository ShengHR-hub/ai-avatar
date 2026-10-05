"""点数钱包与流水模型。

表：`point_wallets`、`point_ledger`
唯一来源：`docs/architecture/02-database-design.md` 第 3.2 节。

- `point_ledger` 只允许 INSERT，是点数事实来源；`point_wallets` 只是同事务更新的余额快照；
- 数据库 CHECK 固定四种标准变化；`ADJUSTMENT` 必须带
  `metadata.adjustment_kind`（`POSITIVE_CREDIT` / `NEGATIVE_CONSUMPTION`）、
  不改变 `reserved_points`，且符号与 kind 一致（`02-database-design.md` 第 3.2 节）；
- `adjustment_kind` 的「必填」必须写成显式 `IS NOT NULL`（迁移 0018）：
  PostgreSQL 的 CHECK 只在结果为 FALSE 时拒绝，缺失键会让
  `metadata ->> 'adjustment_kind' = '...'` 返回 NULL 而被当作通过；
- 账务最终化唯一性由两个部分唯一索引保证：
  每个任务最多一条 `RESERVE`，且 `CONSUME` / `RELEASE` 互斥、总共最多一条。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin

__all__ = ["PointLedger", "PointWallet"]

_ENTRY_TYPE_DELTA_CHECK = """
(entry_type = 'CREDIT' AND available_delta = amount AND reserved_delta = 0)
OR (entry_type = 'RESERVE' AND available_delta = -amount AND reserved_delta = amount)
OR (entry_type = 'CONSUME' AND available_delta = 0 AND reserved_delta = -amount)
OR (entry_type = 'RELEASE' AND available_delta = amount AND reserved_delta = -amount)
OR (
    entry_type = 'ADJUSTMENT'
    AND reserved_delta = 0
    AND metadata ->> 'adjustment_kind' IS NOT NULL
    AND (
        (metadata ->> 'adjustment_kind' = 'POSITIVE_CREDIT' AND available_delta > 0)
        OR (metadata ->> 'adjustment_kind' = 'NEGATIVE_CONSUMPTION' AND available_delta < 0)
    )
)
"""


class PointWallet(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """`point_wallets`：一个用户一个钱包；余额必须非负。"""

    __tablename__ = "point_wallets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    available_points: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )
    reserved_points: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )
    lifetime_credited: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )
    lifetime_consumed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )
    version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )

    __table_args__ = (
        CheckConstraint("available_points >= 0", name="available_points_non_negative"),
        CheckConstraint("reserved_points >= 0", name="reserved_points_non_negative"),
        CheckConstraint("lifetime_credited >= 0", name="lifetime_credited_non_negative"),
        CheckConstraint("lifetime_consumed >= 0", name="lifetime_consumed_non_negative"),
        CheckConstraint("version >= 0", name="version_non_negative"),
    )


class PointLedger(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """`point_ledger`：只允许追加的点数流水，全局唯一的操作级幂等键。"""

    __tablename__ = "point_ledger"

    wallet_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("point_wallets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    available_delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="RESTRICT"),
        nullable=True,
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('CREDIT', 'RESERVE', 'CONSUME', 'RELEASE', 'ADJUSTMENT')",
            name="entry_type",
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(_ENTRY_TYPE_DELTA_CHECK, name="entry_type_delta"),
        Index(
            "uq_point_ledger_generation_reserve",
            "generation_id",
            unique=True,
            postgresql_where=text("entry_type = 'RESERVE'"),
        ),
        Index(
            "uq_point_ledger_generation_final",
            "generation_id",
            unique=True,
            postgresql_where=text("entry_type IN ('CONSUME', 'RELEASE')"),
        ),
        Index("ix_point_ledger_user_id_created_at_id", "user_id", "created_at", "id"),
        Index(
            "ix_point_ledger_wallet_id_created_at_id", "wallet_id", "created_at", "id"
        ),
        Index(
            "ix_point_ledger_generation_id",
            "generation_id",
            postgresql_where=text("generation_id IS NOT NULL"),
        ),
    )
